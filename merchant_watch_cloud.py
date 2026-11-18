#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
《洛克王国：世界》远行商人监控 —— 云端独立版

与本机版共用同一套已验证的解析逻辑（好游快爆官方「远行商人查询器」源站），
差异仅在于：

  · 只走邮件通道 —— 云端没有飞书 CLI 授权，邮件已可独立完成全部通知
  · 凭据全部来自环境变量，便于在 GitHub Actions / systemd 中以 Secret 注入
  · 增加「漏轮检测」—— 记录每日已检查的时段；若发现某轮从未被检查到，
    会主动发一封漏报提醒，而不是静默错过

设计要点（均已在实测中验证）
  · 源站只渲染当前轮，商品 li 的 data-time = 该轮结束时刻，据此反推时段标签
  · 源站会同时渲染上一轮与当前轮，必须按 data-time 分组后再挑选，否则会误报
  · 闭合时段（00:00-08:00，商人不出摊）仍允许检查刚结束的 20-24 轮，
    这样即使夜间整段错过，醒来后仍能补到
  · 匹配采用「精确 + 包含」双模式：棱镜球必须精确等于「棱镜球」，
    否则会误命中「宝匣棱镜球」「织梦棱镜球」等非远行商人商品

运行：
    SMTP_USER=xxx@163.com SMTP_AUTH_CODE=xxxx python3 merchant_watch_cloud.py
"""

import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate

SRC = "https://www.onebiji.com/hykb_tools/comm/lkwgmerchant/preview.php?id=1&immgj=0"
WIKI = "https://rocokingdomworld.org/data/merchant.json"

# ---------- 通知配置 ----------
MAIL_TO = ["19812138373@163.com", "1123404495@qq.com"]
MAIL_SUBJECT = "远行商人来货了"
GAP_SUBJECT = "远行商人监控·漏轮提醒"
SMTP_HOST = "smtp.163.com"
SMTP_PORT = 465

# 状态文件：记录已检查时段 / 已发通知 / 已报漏轮，用于跨次运行去重
STATE_FILE = os.environ.get("MERCHANT_STATE_FILE", "state.json")

# ---------- 目标商品 ----------
# 匹配模式：
#   exact    - 商品名必须完全等于关键词（棱镜球，避免误命中「宝匣棱镜球」等）
#   contains - 商品名包含任一关键词（用于别名，如 祝福项链 / 祝福项坠）
TARGETS = [
    ("炫彩蛋", ["炫彩蛋", "炫彩精灵蛋"], "contains"),
    ("棱镜球", ["棱镜球"], "exact"),
    ("祝福项链", ["祝福项链", "祝福项坠"], "contains"),
    # 血脉秘药用精确匹配：游戏内另有「地系/火系/机械系/龙系血脉秘药」等各系血脉秘药，
    # 它们是不同道具，按需求不纳入播报，故不能用 contains。
    ("首领血脉秘药", ["首领血脉秘药"], "exact"),
    ("奇异血脉秘药", ["奇异血脉秘药"], "exact"),
]


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
BJ = timezone(timedelta(hours=8))
ROUND_SECONDS = 4 * 3600
SLOTS = ["08-12", "12-16", "16-20", "20-24"]


# ============================ 抓取与解析 ============================

def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": SRC})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def slot_from_end_ts(end_ts):
    """由该轮结束时刻反推时段标签，如 (8,12) -> '08-12'

    第四轮结束于 24:00，Unix 时间戳落在次日 00:00，需还原为 '20-24'。
    """
    dt = datetime.fromtimestamp(end_ts, BJ)
    end_h = dt.hour
    if end_h == 0:
        start_h, end_h = 20, 24
    else:
        start_h = (end_h - 4) % 24
    return f"{start_h:02d}-{end_h:02d}"


def slot_start_ts(end_ts):
    """该轮的起始时刻"""
    return end_ts - ROUND_SECONDS


def is_checkable(end_ts, server_now):
    """该轮是否值得检查。

    1) 正在进行中：start <= now < end
    2) 刚结束不超过 8 小时：覆盖闭合时段（00:00-08:00 商人不出摊），
       使夜间整段错过的情况仍能在稍后补到

    超过 8 小时的轮次不再检查，避免拿到过期的"上一轮"造成误报。
    """
    if not end_ts or not server_now:
        return False
    start_ts = slot_start_ts(end_ts)
    if start_ts <= server_now < end_ts:
        return True
    return 0 <= (server_now - end_ts) <= 8 * 3600


def parse_source(html):
    """解析源站商品，按轮次分组。

    返回 (server_now, rounds)，rounds = {round_end_ts: [item, ...]}
    源站的 shop-list 会同时渲染上一轮与当前轮的商品（各自 data-time 不同），
    必须分组后再由调用方挑选。
    """
    m = re.search(r"serverNow\s*=\s*(\d+)", html)
    server_now = int(m.group(1)) if m else None

    seg = re.search(r'<ul class="shop-list">(.*?)</ul>', html, re.S)
    if not seg:
        return server_now, {}
    body = seg.group(1)

    rounds = {}
    for li in re.finditer(r"<li ([^>]*?)>\s*(.*?)</li>", body, re.S):
        attrs = li.group(1)
        if "show_none_tip" in attrs:
            continue
        t = re.search(r'data-time="(\d+)"', attrs)
        if not t:
            continue
        end_ts = int(t.group(1))

        sm = re.search(r"showShopinfo\('[^']*','([^']*)','([^']*)'", attrs)
        if sm:
            name = sm.group(1)
        else:
            nm = re.search(r'shop_name">(.*?)</em>', li.group(2), re.S)
            if not nm:
                continue
            name = re.sub(r"<[^>]+>", "", nm.group(1)).strip()
        if not name:
            continue

        pr = re.search(r'shop_price">(.*?)</em>', li.group(2), re.S)
        price = (re.sub(r"<[^>]+>", "", pr.group(1)).replace("价格：", "").strip()
                 if pr else "")
        lim = re.search(r"限购(\d+)", li.group(2))
        sp = re.search(r'data-special="(\d)"', attrs)
        rounds.setdefault(end_ts, []).append({
            "name": name.strip(),
            "price": price,
            "limit": int(lim.group(1)) if lim else None,
            "hot": (sp.group(1) == "1") if sp else False,
        })
    return server_now, rounds


def match_targets(items):
    """匹配目标商品，返回 [(标准名, 实际商品名)]"""
    hits = []
    for std, keys, mode in TARGETS:
        for it in items:
            nm = (it.get("name") or "").strip()
            if not nm:
                continue
            matched = (nm in keys) if mode == "exact" else any(k in nm for k in keys)
            if matched:
                hits.append((std, nm))
                break
    return hits


def collect():
    """抓取并解析出可检查的那一轮"""
    html = fetch(SRC)
    server_now, rounds = parse_source(html)

    end_ts, items = None, []
    for ets, its in rounds.items():
        if is_checkable(ets, server_now):
            # 优先取正在进行中的轮；同条件下取结束时刻最晚的
            if end_ts is None or ets > end_ts:
                end_ts, items = ets, its
    if end_ts is None and rounds:
        end_ts = max(rounds.keys())
        items = rounds[end_ts]

    result = {
        "ok": True,
        "server_now": server_now,
        "server_now_bj": (datetime.fromtimestamp(server_now, BJ).strftime("%Y-%m-%d %H:%M:%S")
                          if server_now else None),
        "round_end_ts": end_ts,
        "slot": slot_from_end_ts(end_ts) if end_ts else None,
        "items": items,
        "hits": [],
        "checkable": False,
    }
    result["checkable"] = is_checkable(end_ts, server_now)
    if result["checkable"]:
        result["hits"] = match_targets(items)
    return result


# ============================ 状态与去重 ============================

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("seen_slots", [])    # 已检查过的时段键
    s.setdefault("alerts", [])        # 已发出的到货通知键
    s.setdefault("gap_reported", [])  # 已发出的漏轮提醒键
    return s


def save_state(state):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(json.dumps({"warn": f"state save failed: {e}"}, ensure_ascii=False))


def slot_key(end_ts):
    """时段键：起始日期 + 时段标签，跨天不串号"""
    st = datetime.fromtimestamp(slot_start_ts(end_ts), BJ)
    return f"{st.strftime('%Y-%m-%d')}|{slot_from_end_ts(end_ts)}"


# ============================ 邮件发送 ============================

def build_mail_body(slot, items):
    """正文：当前时段售卖商品清单（按要求不标注洛克贝价格）"""
    lines = [f"当前时段 {slot}", ""]
    for it in items:
        name = it.get("name") or ""
        limit = it.get("limit")
        tag = "（热销）" if it.get("hot") else ""
        bits = [f"限购{limit}"] if limit is not None else []
        lines.append(f"· {name}{tag}" + ("  " + "  ".join(bits) if bits else ""))
    lines.append("")
    lines.append("——《洛克王国：世界》远行商人")
    return "\n".join(lines)


def send_mail(subject, body, to_addrs=None):
    """通过 163 SMTP 发送邮件。逐个收件人投递并记录失败项。"""
    user = os.environ.get("SMTP_USER")
    code = os.environ.get("SMTP_AUTH_CODE")
    if not user or not code:
        return False, {"error": "缺少 SMTP_USER / SMTP_AUTH_CODE 环境变量"}

    if to_addrs is None:
        to_addrs = MAIL_TO
    elif isinstance(to_addrs, str):
        to_addrs = [to_addrs]
    to_addrs = [a for a in to_addrs if a]
    if not to_addrs:
        return False, {"error": "无收件人"}

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("洛克王国监控", "utf-8")), user))
    msg["To"] = ", ".join(to_addrs)
    msg["Date"] = formatdate(localtime=True)

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=45) as s:
            s.login(user, code)
            sent_ok, failed = [], []
            for addr in to_addrs:
                try:
                    s.sendmail(user, [addr], msg.as_string())
                    sent_ok.append(addr)
                except Exception as e:
                    failed.append({"addr": addr, "error": f"{type(e).__name__}: {e}"})
        if failed and not sent_ok:
            return False, {"error": "全部收件人投递失败", "failed": failed}
        return True, {"to": sent_ok, "failed": failed}
    except Exception as e:
        return False, {"error": f"{type(e).__name__}: {e}"}


# ============================ 漏轮检测 ============================

def find_gaps(state, server_now):
    """找出已结束、但从未被检查过的时段。

    约束：
      1. 只统计「部署起点」之后结束的时段 —— 否则首次部署时，
         历史上所有从未检查过的时段都会被误报成漏轮。
      2. 只统计「当前自然日」的时段 —— 部署当日不追溯昨日，
         避免刚上线就收到一堆历史补报。

    部署起点取首次运行时写入的 first_run，缺失则退回 24 小时前。
    """
    gaps = []
    since = state.get("first_run")
    if since is None:
        since = server_now - 24 * 3600
    since = int(since)

    now_dt = datetime.fromtimestamp(server_now, BJ)
    today_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)

    for label in SLOTS:
        sh, eh = [int(x) for x in label.split("-")]
        if eh == 24:
            end = today_start + timedelta(days=1)
        else:
            end = today_start.replace(hour=eh)
        start = end - timedelta(seconds=ROUND_SECONDS)
        end_ts = int(end.timestamp())
        if end_ts > server_now:
            continue              # 尚未结束
        if end_ts < since:
            continue              # 部署之前，不追溯
        key = f"{start.strftime('%Y-%m-%d')}|{label}"
        if key not in state["seen_slots"]:
            gaps.append((key, label, start.strftime("%Y-%m-%d")))
    return gaps


# ============================ 主流程 ============================

def main():
    # --- 抓取（源站整点切换可能延迟，做有限重试）---
    data = None
    for attempt in range(3):
        try:
            data = collect()
            break
        except Exception as e:
            if attempt == 2:
                print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
                sys.exit(1)
            time.sleep(3)

    state = load_state()
    # 首次运行时间作为漏轮追溯起点，避免把部署前的历史时段误报为漏轮
    if state.get("first_run") is None and data.get("server_now"):
        state["first_run"] = int(data["server_now"])

    slot = data.get("slot")
    hits = data.get("hits") or []
    items = data.get("items") or []
    server_now = data.get("server_now")

    result = {
        "ok": True,
        "time_bj": data.get("server_now_bj"),
        "slot": slot,
        "items": [i.get("name") for i in items],
        "hits": hits,
        "sent": False,
    }

    # --- 记录本次检查到的时段 ---
    if data.get("checkable") and data.get("round_end_ts"):
        k = slot_key(data["round_end_ts"])
        if k not in state["seen_slots"]:
            state["seen_slots"].append(k)
            state["seen_slots"] = state["seen_slots"][-200:]

    # --- 命中目标商品则通知 ---
    if hits and slot:
        names = [h[1] for h in hits]
        alert_key = f"{slot_key(data['round_end_ts'])}|{'/'.join(names)}"
        if alert_key in state["alerts"]:
            result["note"] = "duplicate"
        else:
            body = build_mail_body(slot, items)
            ok, resp = send_mail(MAIL_SUBJECT, body)
            result["sent"] = ok
            result["mail"] = resp
            if ok:
                state["alerts"].append(alert_key)
                state["alerts"] = state["alerts"][-200:]

    # --- 漏轮检测 ---
    gaps = find_gaps(state, server_now) if server_now else []
    if gaps:
        lines = ["以下时段已结束但未执行检查：", ""]
        for key, label, day in gaps:
            lines.append(f"· {day} {label} 时段")
        lines.append("")
        lines.append("提示：源站仅保留当前轮商品，错过时段的商品无法事后补查。")
        lines.append("——《洛克王国：世界》远行商人监控")
        gap_keys = [g[0] for g in gaps]
        new_gaps = [g for g in gap_keys if g not in state["gap_reported"]]
        if new_gaps:
            ok, resp = send_mail(GAP_SUBJECT, "\n".join(lines))
            result["gap"] = {"slots": new_gaps, "sent": ok, "resp": resp}
            if ok:
                state["gap_reported"].extend(new_gaps)
                state["gap_reported"] = state["gap_reported"][-200:]

    save_state(state)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
