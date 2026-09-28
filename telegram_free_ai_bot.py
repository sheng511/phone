#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
免费 AI 对话 Telegram 机器人（单文件可直接运行 · 高并发优化版）
=================================================
性能优化要点：
  - 全局共享 httpx.AsyncClient（连接池复用，避免每次请求建/拆连接）。
  - 按用户 asyncio.Lock，消除「余额扣减 / 签到 / 结算」的并发竞态。
  - 全局线路延迟缓存（后台任务一次测量，全用户/首页复用），降低探测频率。
  - run_polling 调高 connection_pool_size / 超时，提升 API 吞吐与长轮询稳定性。
  - 所有 I/O 均异步，handler 天然并发；错误兜底不中断主循环。

功能：
  1. 底部按钮切换线路/模型；首次 /start 询问线路；配置完显示「配置成功」。
  2. 线路不通畅自动切最快线路；线路页 🟢/🟡/🔴 显示延迟，可手动切换。
  3. 模型表每日拉取一次，含「🎲 随机最快模型」。
  4. 全局仅一条导航消息（原地编辑）；AI 对话内容独立不被删除；逐级支持返回首页/上一页。
  5. 模型思考内容实时展示，思考结束 5 秒后自动删除。
  6. 防护：密钥仅服务端、白名单 + 封禁 + 维护模式 + 输入截断。
  7. 用户机制(持久化)：每日 +10 机会、可积累、≥50 起 5%/天 计息；签到连周 +10+x、连月 +10+3y（叠加）。
  8. 管理员面板 /admin：统计、用户查询/调额/封禁/重置、广播、封禁名单、线路状态、维护模式、刷新模型。

运行：
  pip install "python-telegram-bot[httpx]"
  export BOT_TOKEN="你的机器人Token"
  python telegram_free_ai_bot.py
"""

import asyncio
import json
import os
import random
import time
from datetime import date, timedelta

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ====================== 配置区 ======================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8655439101:AAECQEVIth2Ugr1y2hxst-ke5douHq-rUU")
OWNER = "nebula511"   # 机器人所有者标识（显示在管理员面板）

# 线路配置：OpenAI 兼容 /v1；api_key 仅服务端使用，绝不外发
LINES = [
    {
        "id": "line1",
        "name": "线路①·yjs",
        "base_url": "https://api.yjs.im/v1",
        "api_key": "sk-k5lTxSHBKbck2q7mAzLoF4vVnrOlBjd8G1ch2Mysa0WE7OGu",
        "health": "/models",
    },
    {
        "id": "line2",
        "name": "线路②·hcnsec",
        "base_url": "https://api.hcnsec.cn/v1",
        "api_key": "sk-qGH1xs05gIXiVVUJ8iCXGGjpcUt6mgztrnQMXExmbAAoAStR",
        "health": "/models",
    },
]

ALLOWED_USERS = []   # 留空=开放（所有人可用）；填 user_id 列表则仅白名单可用
ADMIN_IDS = []        # 管理员 user_id(数字) 列表；与下方用户名名单任一命中即可
ADMIN_USERNAMES = ["nebula511"]   # 管理员用户名（@后部分），用于 /admin 鉴权

MODELS_URL = os.getenv("MODELS_URL", "")
FALLBACK_MODELS = [
    "deepseek-chat", "deepseek-reasoner",
    "gpt-4o-mini", "gpt-3.5-turbo",
    "claude-3-haiku", "gemini-1.5-flash",
    "qwen2.5-7b-instruct", "llama-3.1-8b-instruct",
]

DAILY_QUOTA = 10
INTEREST_RATE = 0.05
INTEREST_MIN = 50
SIGN_BASE = 5

HEALTH_TIMEOUT = 2.5
GREEN_MAX = 400
YELLOW_MAX = 1200
AUTO_SWITCH_INTERVAL = 120
LATENCY_TTL = 30          # 线路延迟缓存有效期(秒)
HISTORY_KEEP = 20
THINK_DELETE_DELAY = 5
MAX_INPUT_LEN = 8000
POOL_SIZE = 128           # Telegram API 连接池大小

BASE = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(BASE, "users.json")
ADMIN_FILE = os.path.join(BASE, "admin.json")
# ===================================================

LINE_BY_ID = {l["id"]: l for l in LINES}


# ---------------- 持久化 ----------------
def load_users() -> dict:
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_users(context: ContextTypes.DEFAULT_TYPE):
    try:
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(context.bot_data.get("ud", {}), f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def load_admin() -> dict:
    try:
        with open(ADMIN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"blocked": [], "maintenance": False}


def save_admin(context: ContextTypes.DEFAULT_TYPE):
    try:
        with open(ADMIN_FILE, "w", encoding="utf-8") as f:
            json.dump(context.bot_data.get("admin", {}), f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------- 共享状态 ----------------
def get_ud(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> dict:
    return context.bot_data.setdefault("ud", {}).setdefault(str(chat_id), {})


def get_admin(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.bot_data.setdefault("admin", load_admin())


def get_lock(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> asyncio.Lock:
    return context.bot_data.setdefault("locks", {}).setdefault(str(chat_id), asyncio.Lock())


def http_client(context: ContextTypes.DEFAULT_TYPE) -> httpx.AsyncClient:
    return context.bot_data["http_client"]


def is_admin(update: Update) -> bool:
    u = update.effective_user
    if not u:
        return False
    if ADMIN_IDS and u.id in ADMIN_IDS:
        return True
    if ADMIN_USERNAMES and u.username in ADMIN_USERNAMES:
        return True
    return False


def is_blocked(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    return str(chat_id) in get_admin(context).get("blocked", [])


def allowed(update: Update) -> bool:
    if not ALLOWED_USERS:
        return True
    return (update.effective_user.id if update.effective_user else None) in ALLOWED_USERS


def ensure_init(ud: dict):
    if "balance" not in ud:
        ud.setdefault("balance", DAILY_QUOTA)
        ud.setdefault("last_settle", date.today().isoformat())
        ud.setdefault("sign_streak", 0)
        ud.setdefault("sign_count", 0)
        ud.setdefault("last_sign", None)


def settle(ud: dict):
    today = date.today()
    last_s = ud.get("last_settle")
    if not last_s:
        ud["last_settle"] = today.isoformat()
        return
    last = date.fromisoformat(last_s)
    days = (today - last).days
    if days <= 0:
        return
    bal = ud.get("balance", 0)
    for _ in range(days):
        bal += DAILY_QUOTA
        if bal >= INTEREST_MIN:
            bal += int(bal * INTEREST_RATE)
    ud["balance"] = bal
    ud["last_settle"] = today.isoformat()


# ---------------- 着色 ----------------
def color_of(ms):
    if ms is None:
        return "🔴", "不通畅"
    if ms <= GREEN_MAX:
        return "🟢", "流畅"
    if ms <= YELLOW_MAX:
        return "🟡", "一般"
    return "🔴", "不通畅"


# ---------------- 健康检查（共享 client + 缓存） ----------------
async def check_line(line, client: httpx.AsyncClient) -> int | None:
    url = line["base_url"].rstrip("/") + (line.get("health") or "/")
    headers = {"Authorization": f"Bearer {line.get('api_key','')}"} if line.get("api_key") else {}
    try:
        t0 = time.time()
        r = await client.get(url, headers=headers, timeout=HEALTH_TIMEOUT)
        dt = int((time.time() - t0) * 1000)
        return dt if r.status_code < 500 else None
    except Exception:
        return None


async def measure_all(client: httpx.AsyncClient) -> dict:
    results = await asyncio.gather(*(check_line(l, client) for l in LINES))
    return {l["id"]: r for l, r in zip(LINES, results)}


async def measure_cached(context: ContextTypes.DEFAULT_TYPE, force: bool = False) -> dict:
    """带 TTL 的全局延迟缓存；force=True 强制刷新（如用户主动查看线路页）。"""
    now = time.time()
    cache = context.bot_data.get("latency_cache")
    if not force and cache and (now - cache["ts"]) < LATENCY_TTL:
        return cache["data"]
    data = await measure_all(http_client(context))
    context.bot_data["latency_cache"] = {"data": data, "ts": now}
    return data


# ---------------- 模型表 ----------------
async def get_models(context: ContextTypes.DEFAULT_TYPE) -> list:
    cache = context.bot_data.setdefault("models", {"date": None, "list": []})
    today = date.today().isoformat()
    if cache["date"] == today and cache["list"]:
        return cache["list"]
    models = list(FALLBACK_MODELS)
    if MODELS_URL:
        try:
            r = await http_client(context).get(MODELS_URL, timeout=8.0)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and data:
                    models = [str(m) for m in data]
        except Exception:
            pass
    cache["date"] = today
    cache["list"] = models
    return models


# ---------------- 流式对话 ----------------
async def stream_chat(client, line_id, model, history, on_think, on_text):
    line = LINE_BY_ID.get(line_id)
    if not line:
        raise ValueError("未知线路")
    url = line["base_url"].rstrip("/") + "/chat/completions"
    payload = {"model": model, "messages": history[-HISTORY_KEEP:], "stream": True}
    headers = {"Authorization": f"Bearer {line['api_key']}", "Content-Type": "application/json"}
    async with client.stream("POST", url, json=payload, headers=headers, timeout=180.0) as r:
        if r.status_code != 200:
            await r.aread()
            raise RuntimeError(f"HTTP {r.status_code}")
        async for raw in r.aiter_lines():
            if not raw or not raw.startswith("data:"):
                continue
            data = raw[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except Exception:
                continue
            if not obj.get("choices"):
                continue
            delta = obj["choices"][0].get("delta", {})
            rc = delta.get("reasoning_content") or delta.get("reasoning") or delta.get("thinking")
            ct = delta.get("content")
            if rc:
                await on_think(rc)
            if ct:
                await on_text(ct)


async def call_api_fallback(client, line_id, model, history) -> str:
    line = LINE_BY_ID.get(line_id)
    if not line:
        return "⚠️ 未知线路"
    url = line["base_url"].rstrip("/") + "/chat/completions"
    payload = {"model": model, "messages": history[-HISTORY_KEEP:], "stream": False}
    headers = {"Authorization": f"Bearer {line['api_key']}", "Content-Type": "application/json"}
    try:
        r = await client.post(url, json=payload, headers=headers, timeout=120.0)
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"]
        return f"⚠️ 接口返回错误：HTTP {r.status_code}"
    except Exception as e:
        return f"⚠️ 调用失败：{e}"


# ---------------- 消息辅助 ----------------
async def safe_edit(context, chat_id, msg_id, text, reply_markup=None):
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=msg_id, text=text[:4000], reply_markup=reply_markup
        )
    except Exception:
        pass


async def send_or_edit(context, chat_id, msg_id, text, reply_markup=None):
    if len(text) <= 4000:
        await safe_edit(context, chat_id, msg_id, text, reply_markup)
        return
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except Exception:
        pass
    parts = [text[i:i + 4000] for i in range(0, len(text), 4000)]
    for i, p in enumerate(parts):
        kb = reply_markup if i == len(parts) - 1 else None
        await context.bot.send_message(chat_id=chat_id, text=p, reply_markup=kb)


async def delete_msg_job(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data
    try:
        await context.bot.delete_message(chat_id=d["chat_id"], message_id=d["msg_id"])
    except Exception:
        pass


# ---------------- 用户导航消息（全局仅一条） ----------------
def chat_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⚙ 切换线路", callback_data="open_lines"),
        InlineKeyboardButton("⚙ 切换模型", callback_data="open_models"),
    ]])


async def render(update: Update, context: ContextTypes.DEFAULT_TYPE, page: str, banner: str = None):
    chat_id = update.effective_chat.id
    ud = get_ud(context, chat_id)
    ud.setdefault("stack", [])
    text, kb = await build_page(page, ud, context, banner)
    nav_id = ud.get("nav_msg_id")
    if nav_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=nav_id, text=text, reply_markup=kb, parse_mode="HTML"
            )
            return
        except Exception:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=nav_id)
            except Exception:
                pass
    msg = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, parse_mode="HTML")
    ud["nav_msg_id"] = msg.message_id
    ud["page"] = page


async def build_page(page, ud, context, banner):
    if page == "home":
        line = LINE_BY_ID.get(ud.get("current_line"))
        model = ud.get("current_model")
        lat = context.bot_data.get("latency_cache", {}).get("data", {})
        line_emoji = color_of(lat.get(ud.get("current_line")))[0] if line else "⚪"
        head = "✅ <b>配置成功！</b>\n\n" if banner else ""
        body = (
            f"🤖 <b>免费 AI 对话</b>\n\n"
            f"当前线路：{line['name'] if line else '未配置'} {line_emoji}\n"
            f"当前模型：{model or '未配置'}\n"
            f"💬 对话机会：<b>{ud.get('balance', 0)}</b>　📅 连签：{ud.get('sign_streak', 0)} 天\n\n"
            f"点击下方按钮切换线路 / 模型，或发送消息开始对话；签到可获取更多机会。"
        )
        text = head + body
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔀 切换线路", callback_data="lines"),
                InlineKeyboardButton("🧠 切换模型", callback_data="models"),
            ],
            [
                InlineKeyboardButton("📅 签到", callback_data="sign"),
                InlineKeyboardButton("📊 我的", callback_data="my"),
            ],
        ])
        return text, kb
    if page == "lines":
        lat = await measure_cached(context, force=True)
        cur = ud.get("current_line")
        rows = []
        for l in LINES:
            ms = lat.get(l["id"])
            emoji, _ = color_of(ms)
            mark = "▶ " if l["id"] == cur else "   "
            label = f"{mark}{l['name']}  {emoji} {ms if ms is not None else '超时'}ms"
            rows.append([InlineKeyboardButton(label, callback_data=f"pick_line:{l['id']}")])
        rows.append([InlineKeyboardButton("🏠 返回首页", callback_data="home")])
        return "🔀 <b>切换线路</b>\n\n🟢 流畅　🟡 一般　🔴 不通畅\n(点击线路即可手动切换)", InlineKeyboardMarkup(rows)
    if page == "models":
        models = await get_models(context)
        cur = ud.get("current_model")
        rows = []
        for i in range(0, len(models), 2):
            chunk = models[i:i + 2]
            btn_row = []
            for m in chunk:
                mark = "▶ " if m == cur else ""
                btn_row.append(InlineKeyboardButton(f"{mark}{m}", callback_data=f"pick_model:{m}"))
            rows.append(btn_row)
        rows.append([InlineKeyboardButton("🎲 随机最快模型", callback_data="random_model")])
        rows.append([InlineKeyboardButton("🏠 返回首页", callback_data="home")])
        return "🧠 <b>切换模型</b>\n\n(模型表每日自动拉取一次，点击选用)", InlineKeyboardMarkup(rows)
    return "🤖 菜单", InlineKeyboardMarkup([[
        InlineKeyboardButton("🔀 切换线路", callback_data="lines"),
        InlineKeyboardButton("🧠 切换模型", callback_data="models"),
    ]])


async def auto_switch_for(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    ud = get_ud(context, chat_id)
    lat = await measure_cached(context)   # 复用全局缓存，不重复探测
    cur = ud.get("current_line")
    if cur and lat.get(cur) is not None and lat.get(cur) <= YELLOW_MAX:
        return False
    avail = [k for k, v in lat.items() if v is not None]
    if not avail:
        return False
    best = min(avail, key=lambda k: lat[k])
    if best != cur:
        ud["current_line"] = best
        nav_id = ud.get("nav_msg_id")
        if nav_id and ud.get("page") == "home":
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=nav_id,
                    text=f"⚡ <b>已自动切换到最快线路：{LINE_BY_ID[best]['name']}</b>\n\n"
                         f"🤖 <b>免费 AI 对话</b>\n\n当前线路：{LINE_BY_ID[best]['name']} {color_of(lat[best])[0]}\n"
                         f"当前模型：{ud.get('current_model','未配置')}\n"
                         f"💬 对话机会：<b>{ud.get('balance',0)}</b>　📅 连签：{ud.get('sign_streak',0)} 天\n\n"
                         f"点击下方按钮切换线路 / 模型，或直接发送消息开始对话。",
                    reply_markup=InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton("🔀 切换线路", callback_data="lines"),
                            InlineKeyboardButton("🧠 切换模型", callback_data="models"),
                        ],
                        [
                            InlineKeyboardButton("📅 签到", callback_data="sign"),
                            InlineKeyboardButton("📊 我的", callback_data="my"),
                        ],
                    ]),
                    parse_mode="HTML",
                )
            except Exception:
                pass
        return True
    return False


# ---------------- 签到 / 我的 ----------------
async def do_sign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ud = get_ud(context, chat_id)
    async with get_lock(context, chat_id):
        settle(ud)
        today = date.today()
        today_s = today.isoformat()
        if ud.get("last_sign") == today_s:
            await context.bot.send_message(chat_id=chat_id, text="✅ 今天已经签到过啦，明天再来～")
            return
        last = ud.get("last_sign")
        if last == (today - timedelta(days=1)).isoformat():
            ud["sign_streak"] = ud.get("sign_streak", 0) + 1
        else:
            ud["sign_streak"] = 1
        ud["last_sign"] = today_s
        ud["sign_count"] = ud.get("sign_count", 0) + 1
        w = ud["sign_streak"] // 7
        m = ud["sign_streak"] // 30
        week_bonus = (10 + w) if w >= 1 else 0
        month_bonus = (10 + 3 * m) if m >= 1 else 0
        reward = SIGN_BASE + week_bonus + month_bonus
        ud["balance"] = ud.get("balance", 0) + reward
        save_users(context)
    msg = f"📅 <b>签到成功！</b>\n\n连续签到：{ud['sign_streak']} 天（累计 {ud['sign_count']} 次）\n"
    if week_bonus:
        msg += f"🟦 连签 {w} 周奖励：+{week_bonus}\n"
    if month_bonus:
        msg += f"🟪 连签 {m} 月奖励：+{month_bonus}\n"
    msg += (f"────────────\n本次获得对话机会：+{reward}\n💬 当前对话机会：<b>{ud['balance']}</b>\n"
            f"（满 {INTEREST_MIN} 起每日 +{int(INTEREST_RATE*100)}% 利息，可积累）")
    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML")


async def do_my(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ud = get_ud(context, chat_id)
    async with get_lock(context, chat_id):
        settle(ud)
        save_users(context)
    signed_today = "已签到 ✅" if ud.get("last_sign") == date.today().isoformat() else "未签到"
    msg = (f"📊 <b>我的账户</b>\n\n💬 对话机会：<b>{ud.get('balance',0)}</b>\n"
           f"📅 连续签到：{ud.get('sign_streak',0)} 天（累计 {ud.get('sign_count',0)} 次）\n今日签到：{signed_today}\n"
           f"🔀 当前线路：{LINE_BY_ID.get(ud.get('current_line'),{}).get('name','未配置')}\n"
           f"🧠 当前模型：{ud.get('current_model','未配置')}\n"
           f"（每日 +{DAILY_QUOTA} 机会；余额≥{INTEREST_MIN} 起每日 +{int(INTEREST_RATE*100)}% 利息）")
    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML")


# ---------------- 管理员面板 ----------------
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ 仅管理员可用。")
        return
    await render_admin(update, context)


async def render_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    adm = get_admin(context)
    text = (f"🛡️ <b>管理员面板</b>　👑 {OWNER}\n\n"
            f"维护模式：{'🟢 已开启' if adm.get('maintenance') else '🔴 关闭'}\n"
            f"封禁人数：{len(adm.get('blocked', []))}\n"
            f"总用户数：{len(context.bot_data.get('ud', {}))}\n\n"
            f"点击下方执行操作：")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 数据统计", callback_data="adm:stats"),
         InlineKeyboardButton("👥 用户查询", callback_data="adm:users")],
        [InlineKeyboardButton("📢 广播", callback_data="adm:broadcast"),
         InlineKeyboardButton("🚫 封禁名单", callback_data="adm:blocked")],
        [InlineKeyboardButton("🔧 线路状态", callback_data="adm:lines"),
         InlineKeyboardButton("⚙ 维护模式", callback_data="adm:maint")],
        [InlineKeyboardButton("🔄 刷新模型", callback_data="adm:models"),
         InlineKeyboardButton("❌ 关闭面板", callback_data="adm:close")],
    ])
    panel_id = adm.get("panel_msg_id")
    if panel_id:
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=panel_id, text=text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=panel_id)
            except Exception:
                pass
    m = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, parse_mode="HTML")
    adm["panel_msg_id"] = m.message_id
    save_admin(context)


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ud_all = context.bot_data.get("ud", {})
    today = date.today().isoformat()
    total = len(ud_all)
    sum_bal = sum(u.get("balance", 0) for u in ud_all.values())
    signed_today = sum(1 for u in ud_all.values() if u.get("last_sign") == today)
    line_dist = {}
    for u in ud_all.values():
        lid = u.get("current_line")
        if lid:
            line_dist[lid] = line_dist.get(lid, 0) + 1
    dist_txt = "　".join(f"{LINE_BY_ID.get(k,{}).get('name',k)}:{v}" for k, v in line_dist.items()) or "无"
    text = ("📊 <b>数据统计</b>\n\n"
            f"总用户数：{total}\n💬 余额总和：{sum_bal}\n📅 今日已签到：{signed_today}\n"
            f"🔀 线路分布：{dist_txt}\n🚫 封禁：{len(get_admin(context).get('blocked', []))}")
    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")


async def admin_blocked_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    blk = get_admin(context).get("blocked", [])
    text = "🚫 <b>封禁名单</b>\n\n" + ("\n".join(blk) if blk else "（空）")
    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")


async def admin_lines(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    lat = await measure_cached(context, force=True)
    rows = []
    for l in LINES:
        ms = lat.get(l["id"])
        emoji, _ = color_of(ms)
        rows.append(f"{l['name']}　{emoji} {ms if ms is not None else '超时'}ms")
    text = "🔧 <b>线路状态</b>\n\n" + "\n".join(rows)
    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")


async def admin_toggle_maint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    adm = get_admin(context)
    adm["maintenance"] = not adm.get("maintenance", False)
    save_admin(context)
    await render_admin(update, context)
    await context.bot.send_message(chat_id=update.effective_chat.id,
                                   text=f"⚙ 维护模式已{'开启' if adm['maintenance'] else '关闭'}。")


async def admin_refresh_models(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data["models"] = {"date": None, "list": []}
    models = await get_models(context)
    await context.bot.send_message(chat_id=update.effective_chat.id,
                                   text=f"🔄 模型表已刷新，共 {len(models)} 个。")


async def admin_show_user(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: str):
    chat_id = update.effective_chat.id
    ud = context.bot_data.get("ud", {}).get(uid)
    adm = get_admin(context)
    if not ud:
        await context.bot.send_message(chat_id=chat_id, text=f"⚠️ 未找到用户 {uid}")
        return
    blocked = uid in adm.get("blocked", [])
    info = (f"👤 <b>用户 {uid}</b>\n\n"
            f"名称：{ud.get('first_name','')} @{ud.get('username','')}\n"
            f"💬 对话机会：{ud.get('balance',0)}\n📅 连签：{ud.get('sign_streak',0)} 天\n"
            f"🔀 线路：{LINE_BY_ID.get(ud.get('current_line'),{}).get('name','未配置')}\n"
            f"🧠 模型：{ud.get('current_model','未配置')}\n"
            f"状态：{'🚫 已封禁' if blocked else '✅ 正常'}")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("余额 +10", callback_data=f"adm:add:{uid}:10"),
         InlineKeyboardButton("余额 +100", callback_data=f"adm:add:{uid}:100")],
        [InlineKeyboardButton("余额 -10", callback_data=f"adm:sub:{uid}:10"),
         InlineKeyboardButton("重置配置", callback_data=f"adm:reset:{uid}")],
        [InlineKeyboardButton("🚫 封禁" if not blocked else "✅ 解封", callback_data=f"adm:ban:{uid}" if not blocked else f"adm:unban:{uid}"),
         InlineKeyboardButton("🔙 返回面板", callback_data="adm:back")],
    ])
    await context.bot.send_message(chat_id=chat_id, text=info, reply_markup=kb, parse_mode="HTML")


async def handle_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE, aw: dict):
    chat_id = update.effective_chat.id
    mode = aw.get("mode")
    context.bot_data.pop("admin_await", None)
    if mode == "user_lookup":
        uid = (update.message.text or "").strip()
        if not uid.isdigit():
            await update.message.reply_text("⚠️ 请输入数字用户ID。")
            return
        await admin_show_user(update, context, uid)
    elif mode == "broadcast":
        content = update.message.text or ""
        sent = 0
        adm = get_admin(context)
        for uid in list(context.bot_data.get("ud", {}).keys()):
            if uid in adm.get("blocked", []):
                continue
            try:
                await context.bot.send_message(chat_id=int(uid), text="📢 <b>管理员广播</b>\n\n" + content, parse_mode="HTML")
                sent += 1
            except Exception:
                pass
        await update.message.reply_text(f"📢 广播完成，送达 {sent} 人。")


# ---------------- 命令 ----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("⛔ 无权限使用。")
        return
    chat_id = update.effective_chat.id
    ud = get_ud(context, chat_id)
    ensure_init(ud)
    if update.effective_user:
        ud.setdefault("username", update.effective_user.username)
        ud.setdefault("first_name", update.effective_user.first_name)
    if not ud.get("line_asked"):
        ud["line_asked"] = True
        await render(update, context, "lines")
    else:
        await render(update, context, "home")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("⛔ 无权限使用。")
        return
    chat_id = update.effective_chat.id
    get_ud(context, chat_id).clear()
    await update.message.reply_text("🧹 已重置配置，发送 /start 重新初始化。")


# ---------------- 回调 ----------------
async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data.startswith("adm:"):
        if not is_admin(update):
            await q.answer("⛔ 仅管理员", show_alert=True)
            return
        act = data[4:]
        if act == "stats":
            await admin_stats(update, context)
        elif act == "blocked":
            await admin_blocked_list(update, context)
        elif act == "lines":
            await admin_lines(update, context)
        elif act == "maint":
            await admin_toggle_maint(update, context)
        elif act == "models":
            await admin_refresh_models(update, context)
        elif act == "close":
            adm = get_admin(context)
            pid = adm.get("panel_msg_id")
            if pid:
                try:
                    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=pid)
                except Exception:
                    pass
                adm.pop("panel_msg_id", None)
                save_admin(context)
        elif act == "users":
            context.bot_data["admin_await"] = {"chat": update.effective_chat.id, "mode": "user_lookup"}
            await context.bot.send_message(chat_id=update.effective_chat.id, text="👥 请发送要查询的用户ID（数字）。")
        elif act == "broadcast":
            context.bot_data["admin_await"] = {"chat": update.effective_chat.id, "mode": "broadcast"}
            await context.bot.send_message(chat_id=update.effective_chat.id, text="📢 请发送广播内容。")
        elif act == "back":
            await render_admin(update, context)
        elif act.startswith("add:") or act.startswith("sub:"):
            _, uid, n = act.split(":")
            ud = context.bot_data.get("ud", {}).get(uid)
            if ud is not None:
                delta = int(n) if act.startswith("add:") else -int(n)
                async with get_lock(context, int(uid)):
                    ud["balance"] = max(0, ud.get("balance", 0) + delta)
                    save_users(context)
                await admin_show_user(update, context, uid)
        elif act.startswith("reset:"):
            uid = act.split(":", 1)[1]
            u = context.bot_data.get("ud", {}).get(uid)
            if u is not None:
                u.pop("current_line", None); u.pop("current_model", None); u.pop("line_asked", None)
                u.pop("history", None); u.pop("nav_msg_id", None)
                save_users(context)
                await admin_show_user(update, context, uid)
        elif act.startswith("ban:") or act.startswith("unban:"):
            uid = act.split(":", 1)[1]
            adm = get_admin(context)
            blk = adm.setdefault("blocked", [])
            if act.startswith("ban:"):
                if uid not in blk:
                    blk.append(uid)
            else:
                if uid in blk:
                    blk.remove(uid)
            save_admin(context)
            await admin_show_user(update, context, uid)
        return

    if not allowed(update):
        await q.answer("⛔ 无权限", show_alert=True)
        return
    ud = get_ud(context, update.effective_chat.id)

    if data == "home":
        ud["stack"] = []
        await render(update, context, "home")
    elif data in ("lines", "open_lines"):
        ud["stack"].append(ud.get("page", "home"))
        await render(update, context, "lines")
    elif data in ("models", "open_models"):
        ud["stack"].append(ud.get("page", "home"))
        await render(update, context, "models")
    elif data == "random_model":
        models = await get_models(context)
        if models:
            ud["current_model"] = random.choice(models)
            await render(update, context, "home", banner="ok")
    elif data == "sign":
        await do_sign(update, context)
    elif data == "my":
        await do_my(update, context)
    elif data.startswith("pick_line:"):
        lid = data.split(":", 1)[1]
        if lid in LINE_BY_ID:
            ud["current_line"] = lid
            if not ud.get("current_model"):
                ud["stack"].append("lines")
                await render(update, context, "models")
            else:
                await render(update, context, "home", banner="ok")
    elif data.startswith("pick_model:"):
        mid = data.split(":", 1)[1]
        ud["current_model"] = mid
        if not ud.get("current_line"):
            ud["stack"].append("models")
            await render(update, context, "lines")
        else:
            await render(update, context, "home", banner="ok")
    elif data == "back":
        prev = ud["stack"].pop() if ud.get("stack") else "home"
        await render(update, context, prev)


# ---------------- AI 对话 ----------------
async def chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if is_admin(update):
        aw = context.bot_data.get("admin_await")
        if aw and aw.get("chat") == chat_id:
            await handle_admin_input(update, context, aw)
            return

    if is_blocked(context, chat_id):
        await update.message.reply_text("🚫 你已被封禁，无法使用。")
        return
    if get_admin(context).get("maintenance") and not is_admin(update):
        await update.message.reply_text("🛠️ 机器人维护中，暂时不可用。")
        return
    if not allowed(update):
        await update.message.reply_text("⛔ 无权限使用。")
        return

    # 按用户加锁：保证结算/扣减/保存的原子性，避免并发竞态导致多扣或重复签到
    async with get_lock(context, chat_id):
        ud = get_ud(context, chat_id)
        ensure_init(ud)
        if update.effective_user:
            ud.setdefault("username", update.effective_user.username)
            ud.setdefault("first_name", update.effective_user.first_name)
        settle(ud)

        text = (update.message.text or "")[:MAX_INPUT_LEN]

        if ud.get("balance", 0) <= 0 and not is_admin(update):
            await update.message.reply_text(
                f"⚠️ 对话机会已用完（当前 {ud.get('balance',0)}）。\n"
                f"每日自动 +{DAILY_QUOTA}；可签到获取；余额≥{INTEREST_MIN} 起每日计息 +{int(INTEREST_RATE*100)}%。\n"
                f"点击下方「📅 签到」或发送 /start 回首页。",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📅 签到", callback_data="sign"),
                    InlineKeyboardButton("🏠 返回首页", callback_data="home"),
                ]]),
            )
            return

        await auto_switch_for(context, chat_id)

        if not ud.get("current_line") or not ud.get("current_model"):
            await update.message.reply_text(
                "⚠️ 尚未完成配置，请先发送 /start 选择线路与模型。",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔀 切换线路", callback_data="open_lines"),
                    InlineKeyboardButton("🧠 切换模型", callback_data="open_models"),
                ]]),
            )
            return

        history = ud.setdefault("history", [])
        history.append({"role": "user", "content": text})
        if len(history) > HISTORY_KEEP:
            history[:] = history[-HISTORY_KEEP:]

        if not is_admin(update):
            ud["balance"] = ud.get("balance", 0) - 1

        client = http_client(context)
        state = {"think": "", "think_id": None, "last": 0.0, "answer": ""}
        think_header = "🤔 思考中..."

        async def on_think(chunk):
            state["think"] += chunk
            now = time.time()
            if state["think_id"] is None:
                m = await update.message.reply_text(think_header)
                state["think_id"] = m.message_id
                state["last"] = now
            elif now - state["last"] >= 0.5:
                await safe_edit(context, chat_id, state["think_id"], think_header + "\n" + state["think"][-3500:])
                state["last"] = now

        async def on_text(chunk):
            state["answer"] += chunk

        reply_msg = await update.message.reply_text("⏳ 生成中...")

        try:
            await stream_chat(client, ud["current_line"], ud["current_model"], history, on_think, on_text)
        except Exception:
            state["answer"] = await call_api_fallback(client, ud["current_line"], ud["current_model"], history)

        final_text = state["answer"].strip() or "(模型未返回内容)"
        await send_or_edit(context, chat_id, reply_msg.message_id, final_text, reply_markup=chat_kb())
        history.append({"role": "assistant", "content": final_text})
        save_users(context)

    if state.get("think_id") is not None:
        context.job_queue.run_once(
            delete_msg_job, THINK_DELETE_DELAY,
            data={"chat_id": chat_id, "msg_id": state["think_id"]},
        )


# ---------------- 后台任务 ----------------
async def job_auto_switch(context: ContextTypes.DEFAULT_TYPE):
    # 全量用户只探测一次线路，结果写入全局缓存供所有人复用
    await measure_cached(context, force=True)
    for cid in list(context.bot_data.get("ud", {}).keys()):
        try:
            ud = get_ud(context, int(cid))
            async with get_lock(context, int(cid)):
                settle(ud)
            await auto_switch_for(context, int(cid))
        except Exception:
            pass
    save_users(context)


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 兜底：单条更新异常不波及主循环
    pass


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connection_pool_size(POOL_SIZE)
        .connect_timeout(15)
        .read_timeout(60)
        .pool_timeout(60)
        .get_updates_connection_pool_size(8)
        .build()
    )
    # 共享连接池客户端（高并发复用 TCP 连接）
    app.bot_data["ud"] = load_users()
    app.bot_data["admin"] = load_admin()
    app.bot_data["http_client"] = httpx.AsyncClient(
        limits=httpx.Limits(max_connections=POOL_SIZE, max_keepalive_connections=POOL_SIZE // 2),
        timeout=httpx.Timeout(60.0),
    )
    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CallbackQueryHandler(cb_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_handler))
    app.job_queue.run_repeating(job_auto_switch, interval=AUTO_SWITCH_INTERVAL, first=AUTO_SWITCH_INTERVAL)

    async def _shutdown(application: Application):
        c = application.bot_data.get("http_client")
        if c:
            await c.aclose()

    app.post_shutdown.append(_shutdown)
    print("🤖 Bot 已启动（高并发模式），按 Ctrl+C 停止。")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
