# -*- coding: utf-8 -*-
"""
FunPay Widget Bot — experimental Telegram Bot API 10.4
Install: pip install aiohttp
Run: BOT_TOKEN="..." python funpay_widget.py

NOTE: sendWidget/widget_query are experimental Bot API 10.4 beta methods.
Deposits/withdrawals are manual requests; this script does not process real payments.
"""
import asyncio, json, logging, os, re, secrets, sqlite3, string
from datetime import datetime, timedelta, timezone
from html import escape

import aiohttp

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("Set BOT_TOKEN environment variable")

ADMINS = {5068550043, 6977407005}
DB_FILE = "funpay.sqlite3"
FREEZE_DAYS = 3
BONUS_STARS = 200
BONUS_START = (10, 1)
BONUS_END = (10, 25)
GIFT_RE = re.compile(r"^(?:https?://)?t\.me/nft/[^\s]+$", re.I)
PRICE_RE = re.compile(r"^[1-9][0-9]*$")
CURRENCIES = {"RUB": "₽", "GRAM": "Gram", "STARS": "Stars", "USDT": "USDT"}
API = f"https://api.telegram.org/beta/bot{BOT_TOKEN}"
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

session = None
offset = 0
states = {}
_temp = {}  # uid -> list[message_id] temporary messages to clean up

widget_html = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent;margin:0;padding:0}
html,body{height:100%}
body{
  background:#fff;color:#000;
  font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Segoe UI',Roboto,sans-serif;
  font-size:14px;font-weight:400;
  -webkit-font-smoothing:antialiased;
  padding:20px 16px;
  display:flex;flex-direction:column;
}
.wrap{width:100%;flex:1;display:flex;flex-direction:column;align-items:stretch}
.brand{
  display:block;text-align:center;
  font-size:10px;font-weight:700;letter-spacing:1.6px;
  color:#3390EC;text-transform:uppercase;text-decoration:none;
  margin-bottom:14px;
}
.brand:active{opacity:.6}
.title{font-size:20px;font-weight:700;letter-spacing:-.4px;line-height:1.25;color:#000;text-align:center;margin-bottom:6px}
.sub{font-size:13px;font-weight:400;color:#8E8E93;line-height:1.45;text-align:center;margin-bottom:20px}
#buttons{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.btn{
  position:relative;overflow:hidden;
  display:flex;align-items:center;justify-content:center;
  width:100%;height:42px;border:0;border-radius:12px;
  background:#EFEFF4;color:#3390EC;
  font-family:inherit;font-size:14px;font-weight:500;
  cursor:pointer;padding:0 12px;
  user-select:none;-webkit-user-select:none;
  transition:transform .12s ease;
}
.btn.primary{background:#3390EC;color:#fff}
.btn.wide{grid-column:1/-1}
.btn::after{
  content:'';position:absolute;inset:0;border-radius:inherit;
  background:#fff;opacity:0;transition:opacity .22s ease;pointer-events:none;
}
.btn:active:not(:disabled)::after{opacity:.45}
.btn.primary:active:not(:disabled)::after{opacity:.16}
.btn:active:not(:disabled){transform:scale(.98)}
.btn:disabled{opacity:.5;cursor:not-allowed}
.result{
  white-space:pre-wrap;margin-top:16px;
  font-size:13px;line-height:1.5;text-align:center;
  color:#3C3C43;
}
.result:empty{display:none}
</style></head><body><div class="wrap">
<a class="brand" href="https://funpay.com" target="_blank" rel="noopener">FUNPAY</a>
<div class="title" id="title">Добро пожаловать</div>
<div class="sub" id="sub">Создавайте сделки и управляйте балансом прямо в Telegram.</div>
<div id="buttons">
<button class="btn primary wide" data-a="create">Создать сделку</button>
<button class="btn" data-a="wallet">Кошелёк</button>
<button class="btn" data-a="bonus">Бонусы</button>
<button class="btn" data-a="about">О сервисе</button>
<button class="btn" data-a="support">Поддержка</button>
</div>
<div class="result" id="result"></div>
</div>
<script>
const $=id=>document.getElementById(id);
function buzz(){try{TelegramWidget.HapticFeedback.impactOccurred('light')}catch(e){}}
function draw(v){
  if(!v)return;
  try{
    let x=JSON.parse(v);
    if(x.title)$('title').textContent=x.title;
    if(x.sub!==undefined)$('sub').textContent=x.sub;
    if(x.result!==undefined)$('result').textContent=x.result;
    if(x.buttons){
      $('buttons').innerHTML='';
      x.buttons.forEach(b=>{
        let e=document.createElement('button');
        e.className='btn '+(b.primary?'primary ':'')+(b.wide?'wide':'');
        e.textContent=b.label;
        e.dataset.a=b.action;
        $('buttons').appendChild(e)
      })
    }
  }catch(e){$('result').textContent=v}
}
$('buttons').addEventListener('click',async e=>{
  let b=e.target.closest('button[data-a]');
  if(!b||b.disabled)return;
  buzz();
  b.disabled=true;
  try{
    let r=await TelegramWidget.request(b.dataset.a);
    if(r&&r.result)draw(r.result)
  }catch(err){
    $('result').textContent='Не удалось выполнить действие: '+(err.code||err.message||'ошибка');
    try{TelegramWidget.HapticFeedback.notificationOccurred('error')}catch(_){}
  }finally{b.disabled=false}
});
try{
  const cd=TelegramWidget.chatData||'';
  if(cd.startsWith('confirm:')){
    const id=cd.split(':')[1];
    draw(JSON.stringify({title:'Подтверждение сделки',sub:'Подтвердите получение подарка только после фактической передачи.',buttons:[{label:'Подтвердить сделку',action:'deal_confirm:'+id,primary:true,wide:true},{label:'На главную',action:'home',wide:true}],result:''}))
  }
  else if(cd==='admin'){
    draw(JSON.stringify({title:'Админ-панель',sub:'Управление FunPay',buttons:[{label:'Управление сделками',action:'work:list',primary:true,wide:true},{label:'На главную',action:'home',wide:true}],result:'Команды: /admin, /work, /p CODE, /txt ТЕКСТ, /profit СУММА, /m СУММА @username, /delbal, /delsdel, /cancel'}))
  }
  else if(cd.startsWith('reply:')){
    const id=cd.split(':')[1];
    draw(JSON.stringify({title:'Ответ поддержки',sub:'Нажмите кнопку и отправьте ответ обычным сообщением.',buttons:[{label:'Написать ответ',action:'admin_reply:'+id,primary:true,wide:true},{label:'На главную',action:'home',wide:true}],result:''}))
  }
  TelegramWidget.ready()
}catch(e){}
</script></body></html>"""

def db():
    c = sqlite3.connect(DB_FILE)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c

def now_iso(): return datetime.now(timezone.utc).isoformat()
def fmt(n): return f"{int(n):,}".replace(",", " ")
def cname(c): return CURRENCIES.get(c, c)
def bcol(c): return {"RUB": "balance", "GRAM": "gram_balance", "STARS": "stars_balance", "USDT": "usdt_balance"}[c]
def hcol(c): return {"RUB": "held_balance", "GRAM": "gram_held_balance", "STARS": "stars_held_balance", "USDT": "usdt_held_balance"}[c]

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY,username TEXT,first_name TEXT,balance INTEGER NOT NULL DEFAULT 0,held_balance INTEGER NOT NULL DEFAULT 0,gram_balance INTEGER NOT NULL DEFAULT 0,gram_held_balance INTEGER NOT NULL DEFAULT 0,stars_balance INTEGER NOT NULL DEFAULT 0,stars_held_balance INTEGER NOT NULL DEFAULT 0,usdt_balance INTEGER NOT NULL DEFAULT 0,usdt_held_balance INTEGER NOT NULL DEFAULT 0,first_paid_at TEXT,bonus_claimed INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS deals(id INTEGER PRIMARY KEY AUTOINCREMENT,code TEXT UNIQUE,creator_id INTEGER NOT NULL,payer_id INTEGER,gift_url TEXT NOT NULL,amount INTEGER NOT NULL,currency TEXT NOT NULL DEFAULT 'RUB',status TEXT NOT NULL DEFAULT 'pending',created_at TEXT NOT NULL,paid_at TEXT,confirmed_at TEXT);
        CREATE TABLE IF NOT EXISTS operations(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,kind TEXT NOT NULL,amount INTEGER NOT NULL,currency TEXT NOT NULL DEFAULT 'RUB',deal_id INTEGER,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS support_requests(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS balance_holds(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,amount INTEGER NOT NULL,currency TEXT NOT NULL DEFAULT 'RUB',deal_id INTEGER NOT NULL,created_at TEXT NOT NULL,release_at TEXT NOT NULL,released INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS deposit_requests(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,currency TEXT NOT NULL,amount INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'pending',created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS withdrawal_requests(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,currency TEXT NOT NULL,amount INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'pending',created_at TEXT NOT NULL);
        """)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(deals)")}
        for name, definition in [("code", "TEXT"), ("confirmed_at", "TEXT"), ("currency", "TEXT NOT NULL DEFAULT 'RUB'")]:
            if name not in cols: c.execute(f"ALTER TABLE deals ADD COLUMN {name} {definition}")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_deals_code ON deals(code)")
        c.commit()

def ensure_user(u):
    with db() as c:
        c.execute("""INSERT INTO users(user_id,username,first_name,created_at) VALUES(?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,first_name=excluded.first_name""",
        (u["id"], u.get("username"), u.get("first_name"), now_iso()))

def gen_code(c):
    while True:
        code = "FP-" + ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
        if not c.execute("SELECT 1 FROM deals WHERE code=?", (code,)).fetchone(): return code

def release_holds():
    now = datetime.now(timezone.utc)
    with db() as c:
        rows = c.execute("SELECT * FROM balance_holds WHERE released=0").fetchall()
        for h in rows:
            try:
                t = datetime.fromisoformat(h["release_at"])
                if t.tzinfo is None: t = t.replace(tzinfo=timezone.utc)
            except Exception: continue
            if t > now: continue
            bc, hc = bcol(h["currency"]), hcol(h["currency"])
            cur = c.execute(f"UPDATE users SET {hc}={hc}-?,{bc}={bc}+? WHERE user_id=? AND {hc}>=?",
                            (h["amount"], h["amount"], h["user_id"], h["amount"]))
            if cur.rowcount:
                c.execute("UPDATE balance_holds SET released=1 WHERE id=?", (h["id"],))
                c.execute("INSERT INTO operations(user_id,kind,amount,currency,deal_id,created_at) VALUES(?,?,?,?,?,?)",
                          (h["user_id"], "hold_released", h["amount"], h["currency"], h["deal_id"], now_iso()))
        c.commit()

def balance(uid, curr="RUB", held=False):
    release_holds()
    col = hcol(curr) if held else bcol(curr)
    with db() as c:
        r = c.execute(f"SELECT {col} v FROM users WHERE user_id=?", (uid,)).fetchone()
        return int(r["v"]) if r else 0

def completed(uid):
    with db() as c: return bool(c.execute("SELECT 1 FROM deals WHERE status='confirmed' AND (creator_id=? OR payer_id=?)", (uid, uid)).fetchone())
def bonus_ok():
    n = datetime.now()
    return n.month == BONUS_START[0] and BONUS_START[1] <= n.day <= BONUS_END[1]

async def api(method, data=None):
    async with session.post(f"{API}/{method}", json=data or {}) as r:
        obj = await r.json(content_type=None)
        if not obj.get("ok"): raise RuntimeError(f"{method}: {obj.get('description')}")
        return obj.get("result")

async def send_message(chat_id, text, **kw):
    data = {"chat_id": chat_id, "text": text}
    data.update(kw)
    return await api("sendMessage", data)

async def send_widget(chat_id, chat_data="home"):
    # sendWidget is a Bot API 10.4 beta method.
    # Размер виджета: width / height. Меняй под себя.
    return await api("sendWidget", {"chat_id": chat_id, "html": widget_html,
                                    "width": 340, "height": 220,
                                    "allow_queries": True, "chat_data": chat_data})

async def delete_message(chat_id, message_id):
    try:
        await api("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
    except Exception:
        pass

async def clear_temp(uid):
    """Delete tracked prompt / validation messages for this user."""
    ids = _temp.pop(uid, [])
    for mid in ids:
        await delete_message(uid, mid)

async def send_temp(uid, text, **kw):
    """Send a temporary message that will be auto-deleted on the next state change."""
    r = await send_message(uid, text, **kw)
    if isinstance(r, dict) and "message_id" in r:
        _temp.setdefault(uid, []).append(r["message_id"])
    return r

async def prompt(uid, text, state):
    await clear_temp(uid)
    states[uid] = state
    await send_temp(uid, text)

def result(title, sub="", buttons=None, body=""):
    return json.dumps({"title": title, "sub": sub, "buttons": buttons or [], "result": body}, ensure_ascii=False, separators=(",", ":"))
def btn(label, action, primary=False, wide=False): return {"label": label, "action": action, "primary": primary, "wide": wide}
HOME = [btn("Создать сделку", "create", True, True), btn("Кошелёк", "wallet"), btn("Бонусы", "bonus"), btn("О сервисе", "about"), btn("Поддержка", "support")]
BACK = [btn("На главную", "home", wide=True)]

async def answer_widget(qid, data):
    # Bot API 10.4: answerWidgetQuery uses widget_query_id + data.
    try: await api("answerWidgetQuery", {"widget_query_id": qid, "data": data[:1024]})
    except Exception: logging.exception("answerWidgetQuery failed")

async def widget_action(upd):
    qid = upd["id"]; u = upd["from"]; uid = u["id"]; data = upd.get("data", "")
    ensure_user(u)
    # User interacted with widget — abandon any pending text-input state and clean old prompts.
    states.pop(uid, None)
    await clear_temp(uid)
    try:
        if data == "home":
            release_holds()
            vals = [balance(uid, x) for x in ("RUB", "STARS", "GRAM", "USDT")]
            active = 0
            with db() as c: active = c.execute("SELECT COUNT(*) n FROM deals WHERE creator_id=? AND status IN ('pending','paid')", (uid,)).fetchone()["n"]
            body = f"Баланс: {fmt(vals[0])} ₽\n{fmt(vals[1])} Stars • {fmt(vals[2])} Gram • {fmt(vals[3])} USDT\nНа удержании: {fmt(balance(uid,'RUB',True))} ₽\nАктивных сделок: {active}"
            out = result("Добро пожаловать на FunPay", "Безопасные сделки и управление балансом.", HOME, body)
        elif data == "create":
            await prompt(uid, "Пришлите ссылку на подарок в формате t.me/nft/...", "waiting_gift")
            out = result("Создание сделки", "Жду ссылку на NFT. Отправьте её обычным сообщением.", BACK)
        elif data == "wallet":
            release_holds()
            body = "\n".join([f"{cname(x)}: {fmt(balance(uid,x))}" for x in CURRENCIES])
            body += f"\n\nНа удержании: {fmt(balance(uid,'RUB',True))} ₽"
            out = result("Кошелёк", "Ваши балансы", [btn("Вывод", "withdraw"), btn("Пополнение", "deposit"), *BACK], body)
        elif data == "deposit":
            out = result("Пополнение", "Выберите валюту", [btn(x, f"depcur:{x}") for x in CURRENCIES] + BACK)
        elif data.startswith("depcur:"):
            curr = data.split(":")[1]
            if curr not in CURRENCIES: raise ValueError("Неизвестная валюта")
            await prompt(uid, f"Введите сумму пополнения в {cname(curr)} целым числом.", "deposit:" + curr)
            out = result("Пополнение", f"Выбрана валюта {cname(curr)}. Введите сумму сообщением.", BACK)
        elif data == "withdraw":
            out = result("Вывод средств", "Выберите валюту", [btn(x, f"withcur:{x}") for x in CURRENCIES] + BACK)
        elif data.startswith("withcur:"):
            curr = data.split(":")[1]
            if curr not in CURRENCIES: raise ValueError("Неизвестная валюта")
            await prompt(uid, f"Введите сумму вывода в {cname(curr)}. Доступно: {fmt(balance(uid,curr))}.", "withdraw:" + curr)
            out = result("Вывод средств", f"Доступно: {fmt(balance(uid,curr))} {cname(curr)}. Введите сумму сообщением.", BACK)
        elif data == "bonus":
            with db() as c:
                r = c.execute("SELECT bonus_claimed FROM users WHERE user_id=?", (uid,)).fetchone()
            claimed = bool(r and r["bonus_claimed"])
            ready = completed(uid)
            body = "Акция 1–25 октября: первая завершённая сделка даёт 200 Telegram Stars."
            label = "Уже получен" if claimed else ("Забрать +200 Stars" if ready and bonus_ok() else "Условия пока не выполнены")
            out = result("Бонусы", "Награда за первую завершённую сделку.", [btn(label, "bonus_claim", True, True), *BACK], body)
        elif data == "bonus_claim":
            with db() as c:
                r = c.execute("SELECT bonus_claimed FROM users WHERE user_id=?", (uid,)).fetchone()
                if not r or r["bonus_claimed"]: raise ValueError("Бонус уже получен.")
                if not bonus_ok() or not c.execute("SELECT 1 FROM deals WHERE status='confirmed' AND (creator_id=? OR payer_id=?)", (uid, uid)).fetchone(): raise ValueError("Акция недоступна или сделка ещё не завершена.")
                c.execute("UPDATE users SET stars_balance=stars_balance+?,bonus_claimed=1 WHERE user_id=? AND bonus_claimed=0", (BONUS_STARS, uid))
                c.execute("INSERT INTO operations(user_id,kind,amount,currency,created_at) VALUES(?,?,?,?,?)", (uid, "october_bonus", BONUS_STARS, "STARS", now_iso()))
                c.commit()
            out = result("Бонус получен", "На баланс начислено +200 Telegram Stars.", BACK)
        elif data == "about":
            out = result("О сервисе", "FunPay — интерфейс для создания сделок с NFT и управления балансом. Все операции доступны в Telegram.", BACK)
        elif data == "support":
            out = result("Поддержка", "Опишите проблему и укажите код сделки, если он есть.", [btn("Написать оператору", "support_write", True, True), *BACK])
        elif data == "support_write":
            await prompt(uid, "Напишите сообщение для поддержки следующим сообщением.", "support_message")
            out = result("Сообщение в поддержку", "Жду ваше сообщение в чате.", BACK)
        elif data.startswith("admin_reply:"):
            if uid not in ADMINS: raise ValueError("Нет доступа.")
            target = int(data.split(":")[1])
            await prompt(uid, "Введите ответ пользователю следующим сообщением. Для отмены: /cancel",
                         {"state": "support_reply", "user_id": target})
            out = result("Ответ поддержки", "Теперь отправьте текст ответа обычным сообщением.", BACK)
        elif data.startswith("deal_confirm:"):
            did = int(data.split(":")[1])
            with db() as c:
                d = c.execute("SELECT * FROM deals WHERE id=?", (did,)).fetchone()
                if not d or d["payer_id"] != uid or d["status"] != "paid": raise ValueError("Сделка недоступна для подтверждения.")
                stamp = now_iso()
                cur = c.execute("UPDATE deals SET status='confirmed',confirmed_at=? WHERE id=? AND status='paid'", (stamp, did))
                if cur.rowcount != 1: raise ValueError("Сделка уже обработана.")
                hc = hcol(d["currency"])
                c.execute(f"UPDATE users SET {hc}={hc}+?,first_paid_at=COALESCE(first_paid_at,?) WHERE user_id=?", (d["amount"], stamp, d["creator_id"]))
                release = (datetime.now(timezone.utc) + timedelta(days=FREEZE_DAYS)).isoformat()
                c.execute("INSERT INTO balance_holds(user_id,amount,currency,deal_id,created_at,release_at) VALUES(?,?,?,?,?,?)", (d["creator_id"], d["amount"], d["currency"], did, stamp, release))
                c.execute("INSERT INTO operations(user_id,kind,amount,currency,deal_id,created_at) VALUES(?,?,?,?,?,?)", (d["creator_id"], "deal_confirmed_hold", d["amount"], d["currency"], did, stamp))
                c.commit()
            await send_message(d["creator_id"], f"Сделка {d['code']} подтверждена. {fmt(d['amount'])} {cname(d['currency'])} помещены на удержание на {FREEZE_DAYS} дня.")
            out = result("Сделка подтверждена", "Средства помещены на удержание.", BACK)
        elif data.startswith("work:"):
            if uid not in ADMINS: raise ValueError("Нет доступа.")
            out = await admin_widget_action(uid, data)
        else:
            out = result("FunPay", "Действие не распознано.", HOME)
    except Exception as e:
        out = result("Не получилось", str(e), BACK)
    await answer_widget(qid, out)

async def admin_widget_action(uid, data):
    if data == "work:list":
        with db() as c: rows = c.execute("SELECT id,code,status,amount,currency FROM deals ORDER BY id DESC LIMIT 10").fetchall()
        buttons = [btn(f"#{r['id']} {r['code']} · {r['status']}", f"work:open:{r['id']}") for r in rows]
        return result("Управление сделками", "Последние 10 сделок", [*buttons, btn("На главную", "home", wide=True)])
    if data.startswith("work:open:"):
        did = int(data.split(":")[-1])
        with db() as c: d = c.execute("SELECT * FROM deals WHERE id=?", (did,)).fetchone()
        if not d: return result("Ошибка", "Сделка не найдена.", [btn("Назад", "work:list", wide=True)])
        return result(f"Сделка #{did}", f"{d['code']} • {d['status']}\nПродавец: {d['creator_id']}\nПокупатель: {d['payer_id'] or '—'}\nNFT: {d['gift_url']}\nСумма: {fmt(d['amount'])} {cname(d['currency'])}",
          [btn("Оплатить", f"work:pay:{did}", True), btn("Отменить", f"work:cancel:{did}"), btn("Удалить", f"work:delete:{did}"), btn("Назад", "work:list")])
    if data.startswith("work:pay:"):
        did = int(data.split(":")[-1])
        with db() as c:
            d = c.execute("SELECT * FROM deals WHERE id=?", (did,)).fetchone()
            if not d or d["status"] != "pending": raise ValueError("Сделка не найдена или уже обработана.")
            if d["creator_id"] == uid: raise ValueError("Нельзя оплатить свою сделку.")
            c.execute("UPDATE deals SET status='paid',payer_id=?,paid_at=? WHERE id=? AND status='pending'", (uid, now_iso(), did)); c.commit()
        await send_message(d["creator_id"], f"Администратор оплатил сделку {d['code']}.")
        return result("Готово", "Сделка помечена оплаченной (без списания баланса).", [btn("К списку", "work:list", wide=True)])
    if data.startswith("work:cancel:"):
        did = int(data.split(":")[-1])
        with db() as c:
            d = c.execute("SELECT * FROM deals WHERE id=?", (did,)).fetchone()
            if not d: raise ValueError("Сделка не найдена.")
            if d["status"] in ("confirmed", "cancelled"): raise ValueError("Эту сделку нельзя отменить.")
            if d["status"] == "paid" and d["payer_id"] and d["payer_id"] not in ADMINS:
                c.execute(f"UPDATE users SET {bcol(d['currency'])}={bcol(d['currency'])}+? WHERE user_id=?", (d["amount"], d["payer_id"]))
                c.execute("INSERT INTO operations(user_id,kind,amount,currency,deal_id,created_at) VALUES(?,?,?,?,?,?)", (d["payer_id"], "deal_refund", d["amount"], d["currency"], did, now_iso()))
            c.execute("UPDATE deals SET status='cancelled' WHERE id=?", (did,)); c.commit()
        return result("Сделка отменена", "Статус обновлён.", [btn("К списку", "work:list", wide=True)])
    if data.startswith("work:delete:"):
        did = int(data.split(":")[-1])
        with db() as c:
            d = c.execute("SELECT status FROM deals WHERE id=?", (did,)).fetchone()
            if not d: raise ValueError("Сделка не найдена.")
            if d["status"] == "confirmed": raise ValueError("Подтверждённую сделку удалять нельзя.")
            c.execute("DELETE FROM deals WHERE id=?", (did,)); c.execute("DELETE FROM operations WHERE deal_id=?", (did,)); c.execute("DELETE FROM balance_holds WHERE deal_id=?", (did,)); c.commit()
        return result("Удалено", "Сделка удалена.", [btn("К списку", "work:list", wide=True)])
    return result("Админ-панель", "Команда не найдена.", [btn("Сделки", "work:list"), *BACK])

async def handle_text(m):
    u = m.get("from", {}); uid = u.get("id"); text = m.get("text", "")
    if not uid or not text: return
    ensure_user(u)
    if text.startswith("/start"):
        states.pop(uid, None)
        await clear_temp(uid)
        try: await send_widget(m["chat"]["id"])
        except Exception as e:
            logging.exception("sendWidget failed")
            await send_message(m["chat"]["id"], "Не удалось отправить экспериментальный виджет. Возможно, Bot API 10.4 ещё недоступен для этого бота/чата.\n" + str(e))
        return
    if text.startswith("/cancel"):
        states.pop(uid, None)
        await clear_temp(uid)
        await send_message(uid, "Действие отменено.")
        return
    if text.startswith("/admin"):
        if uid not in ADMINS: await send_message(uid, "Нет доступа."); return
        states.pop(uid, None); await clear_temp(uid)
        await send_widget(uid, "admin"); await send_message(uid, "Админ-команды: /work, /p CODE, /txt ТЕКСТ, /profit СУММА, /m СУММА @username, /delbal, /delsdel, /cancel"); return
    if text.startswith("/work"):
        if uid not in ADMINS: await send_message(uid, "Нет доступа."); return
        states.pop(uid, None); await clear_temp(uid)
        await send_widget(uid, "admin"); return
    if text.startswith("/p "):
        code = text.split(maxsplit=1)[1].strip().upper()
        with db() as c:
            d = c.execute("SELECT * FROM deals WHERE UPPER(code)=?", (code,)).fetchone()
            if not d: await send_message(uid, "Сделка не найдена."); return
            if d["status"] != "pending": await send_message(uid, "Сделка уже обработана."); return
            if d["creator_id"] == uid: await send_message(uid, "Нельзя оплатить собственную сделку."); return
            amount = int(d["amount"]); curr = d["currency"]
            if uid not in ADMINS:
                cur = c.execute(f"UPDATE users SET {bcol(curr)}={bcol(curr)}-? WHERE user_id=? AND {bcol(curr)}>=?", (amount, uid, amount))
                if cur.rowcount != 1: await send_message(uid, f"Недостаточно средств. Нужно {fmt(amount)} {cname(curr)}."); return
            cur = c.execute("UPDATE deals SET status='paid',payer_id=?,paid_at=? WHERE id=? AND status='pending'", (uid, now_iso(), d["id"]))
            if cur.rowcount != 1:
                if uid not in ADMINS: c.execute(f"UPDATE users SET {bcol(curr)}={bcol(curr)}+? WHERE user_id=?", (amount, uid))
                await send_message(uid, "Сделка уже оплачена."); return
            if uid not in ADMINS: c.execute("INSERT INTO operations(user_id,kind,amount,currency,deal_id,created_at) VALUES(?,?,?,?,?,?)", (uid, "deal_purchase", -amount, curr, d["id"], now_iso()))
            c.commit()
        await send_message(uid, "Если вы получили подарок, подтвердите сделку кнопкой в виджете.", reply_markup=None)
        await send_widget(uid, f"confirm:{d['id']}")
        await send_message(d["creator_id"], f"Покупатель оплатил сделку {d['code']}. После передачи подарка он подтвердит её.")
        return
    if text.startswith("/txt ") and uid in ADMINS:
        body = text.split(maxsplit=1)[1]
        with db() as c: users = c.execute("SELECT user_id FROM users").fetchall()
        ok = bad = 0
        for r in users:
            try: await send_message(r["user_id"], "Сообщение от администрации\n\n" + body); ok += 1; await asyncio.sleep(.04)
            except Exception: bad += 1
        await send_message(uid, f"Рассылка завершена. Отправлено: {ok}; ошибок: {bad}."); return
    if text.startswith("/profit ") and uid in ADMINS:
        try: amount = int(text.split(maxsplit=1)[1])
        except: await send_message(uid, "Формат: /profit 50000"); return
        with db() as c: users = c.execute("SELECT user_id FROM users").fetchall()
        msg = f"В боте только что произошла большая сделка ({fmt(amount)} рублей)\n{datetime.now():%d.%m.%Y %H:%M:%S}"
        ok = bad = 0
        for r in users:
            try: await send_message(r["user_id"], msg); ok += 1; await asyncio.sleep(.04)
            except: bad += 1
        await send_message(uid, f"Отправлено: {ok}; ошибок: {bad}."); return
    if text.startswith("/m ") and uid in ADMINS:
        p = text.split()
        if len(p) != 3: await send_message(uid, "Формат: /m СУММА @username"); return
        try: amount = int(p[1])
        except: await send_message(uid, "Неверная сумма."); return
        if amount <= 0: await send_message(uid, "Сумма должна быть больше нуля."); return
        name = p[2].lstrip("@").lower()
        with db() as c:
            user = c.execute("SELECT * FROM users WHERE LOWER(username)=?", (name,)).fetchone()
            if not user: await send_message(uid, "Пользователь не найден."); return
            c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, user["user_id"]))
            c.execute("INSERT INTO operations(user_id,kind,amount,currency,created_at) VALUES(?,?,?,?,?)", (user["user_id"], "admin_credit", amount, "RUB", now_iso())); c.commit()
        await send_message(uid, f"Начислено {fmt(amount)} ₽ пользователю @{name}.")
        await send_message(user["user_id"], f"Администратор начислил вам {fmt(amount)} ₽."); return
    if uid in ADMINS and text.startswith("/delbal"):
        with db() as c:
            c.execute("UPDATE users SET balance=0,held_balance=0,gram_balance=0,gram_held_balance=0,stars_balance=0,stars_held_balance=0,usdt_balance=0,usdt_held_balance=0")
            c.execute("UPDATE balance_holds SET released=1 WHERE released=0"); c.commit()
        await send_message(uid, "Балансы очищены."); return
    if uid in ADMINS and text.startswith("/delsdel"):
        with db() as c: c.execute("DELETE FROM deals"); c.execute("DELETE FROM operations"); c.execute("DELETE FROM balance_holds"); c.commit()
        await send_message(uid, "Все сделки удалены."); return

    state = states.get(uid)
    if not state: return
    if state == "waiting_gift":
        if not GIFT_RE.fullmatch(text.strip()):
            await send_temp(uid, "Ссылка неверна. Формат: t.me/nft/...")
            return
        states[uid] = {"state": "waiting_price", "gift": text.strip()}
        await clear_temp(uid)
        await send_temp(uid, "Введите цену сделки в рублях целым числом.")
        return
    if isinstance(state, dict) and state["state"] == "waiting_price":
        if not PRICE_RE.fullmatch(text.strip()):
            await send_temp(uid, "Цена должна быть целым числом больше 0.")
            return
        with db() as c:
            code = gen_code(c)
            cur = c.execute("INSERT INTO deals(code,creator_id,gift_url,amount,currency,status,created_at) VALUES(?,?,?,?,'RUB','pending',?)", (code, uid, state["gift"], int(text), now_iso()))
            c.commit()
        states.pop(uid, None)
        await clear_temp(uid)
        await send_message(uid, f"СДЕЛКА FUNPAY\n{state['gift']}\n{fmt(int(text))} ₽\nКод: {code}\nПередайте код покупателю командой /p {code}.")
        return
    if isinstance(state, str) and state == "support_message":
        with db() as c:
            cur = c.execute("INSERT INTO support_requests(user_id,created_at) VALUES(?,?)", (uid, now_iso()))
            rid = cur.lastrowid; c.commit()
        states.pop(uid, None)
        await clear_temp(uid)
        for aid in ADMINS:
            try: await send_message(aid, f"Новое сообщение поддержки #{rid}\nПользователь: @{u.get('username','—')} ({uid})\n\n{text}")
            except: logging.exception("support notify")
        await send_message(uid, "Сообщение отправлено в поддержку."); return
    if isinstance(state, str) and state.startswith("deposit:"):
        curr = state.split(":")[1]
        if not text.isdigit() or int(text) <= 0:
            await send_temp(uid, "Введите целое число больше 0.")
            return
        amount = int(text)
        with db() as c:
            cur = c.execute("INSERT INTO deposit_requests(user_id,currency,amount,created_at) VALUES(?,?,?,?)", (uid, curr, amount, now_iso())); rid = cur.lastrowid; c.commit()
        states.pop(uid, None)
        await clear_temp(uid)
        await send_message(uid, f"Заявка на пополнение #{rid}: {fmt(amount)} {cname(curr)}. Ожидайте оператора.")
        for aid in ADMINS:
            try: await send_message(aid, f"Заявка на пополнение #{rid}\nПользователь {uid}\n{fmt(amount)} {cname(curr)}")
            except: pass
        return
    if isinstance(state, str) and state.startswith("withdraw:"):
        curr = state.split(":")[1]
        if not text.isdigit() or int(text) <= 0:
            await send_temp(uid, "Введите целое число больше 0.")
            return
        amount = int(text)
        with db() as c:
            cur = c.execute(f"UPDATE users SET {bcol(curr)}={bcol(curr)}-? WHERE user_id=? AND {bcol(curr)}>=?", (amount, uid, amount))
            if cur.rowcount != 1:
                await send_temp(uid, "Недостаточно средств.")
                return
            cur = c.execute("INSERT INTO withdrawal_requests(user_id,currency,amount,created_at) VALUES(?,?,?,?)", (uid, curr, amount, now_iso())); rid = cur.lastrowid
            c.execute("INSERT INTO operations(user_id,kind,amount,currency,created_at) VALUES(?,?,?,?,?)", (uid, "withdraw_request", -amount, curr, now_iso())); c.commit()
        states.pop(uid, None)
        await clear_temp(uid)
        await send_message(uid, f"Заявка на вывод #{rid}: {fmt(amount)} {cname(curr)}. Ожидайте оператора.")
        for aid in ADMINS:
            try: await send_message(aid, f"Заявка на вывод #{rid}\nПользователь {uid}\n{fmt(amount)} {cname(curr)}")
            except: pass
        return
    if isinstance(state, dict) and state.get("state") == "support_reply":
        target = state["user_id"]; states.pop(uid, None)
        await clear_temp(uid)
        await send_message(target, "Ответ поддержки FunPay:\n\n" + text)
        await send_message(uid, "Ответ отправлен."); return

async def poll():
    global offset
    while True:
        try:
            updates = await api("getUpdates", {"offset": offset, "timeout": 25, "allowed_updates": ["message", "widget_query", "shared_widget_query"]})
            for up in updates:
                offset = up["update_id"] + 1
                if "message" in up: await handle_text(up["message"])
                elif "widget_query" in up: await widget_action(up["widget_query"])
                elif "shared_widget_query" in up: await widget_action(up["shared_widget_query"])
        except asyncio.CancelledError: raise
        except Exception:
            logging.exception("Polling error")
            await asyncio.sleep(2)

async def release_loop():
    while True:
        try: release_holds()
        except Exception: logging.exception("release holds")
        await asyncio.sleep(60)

async def main():
    global session
    init_db()
    timeout = aiohttp.ClientTimeout(total=35)
    session = aiohttp.ClientSession(timeout=timeout)
    try:
        me = await api("getMe")
        logging.info("Bot started: @%s", me.get("username"))
        await asyncio.gather(poll(), release_loop())
    finally:
        await session.close()

if __name__ == "__main__":
    asyncio.run(main())
