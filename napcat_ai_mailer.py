#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
napcat_ai_mailer_v3_final.py
功能：
 1. AI 返回结构化 JSON，后端解析成邮件 HTML
 2. 邮件支持分块折叠
 3. 数据库支持批量删除、批量标记
 4. 配置页面完整修复，支持永久保存和实时生效
 5. 所有操作都有浮动通知反馈
"""
import os
import json
import sqlite3
import threading
import time
import traceback
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import smtplib
import requests
import websocket
import schedule
from flask import Flask, request, jsonify, render_template_string, redirect, url_for
import signal

DEFAULT_CONFIG = {
    "WS_URL": "ws://127.0.0.1:3001",
    "API_BASE_URL": "http://127.0.0.1:3000",
    "DB_FILE": "./napcat_messages.db",
    "AI_API_URL": "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
    "AI_API_KEY": "",
    "AI_MODEL": "",
    "SMTP_HOST": "smtp.gmail.com",
    "SMTP_PORT": 587,
    "SMTP_USER": "",
    "SMTP_PASSWORD": "",
    "EMAIL_FROM": "",
    "EMAIL_TO": "",
    "EMAIL_SENDER_NAME": "NapCat💌助手",
    "RUN_TIMES": ["09:00", "12:00", "18:00"],
    "BATCH_MAX_MESSAGES": 200,
    "FLASK_HOST": "0.0.0.0",
    "FLASK_PORT": 8080,
    "PAGE_SIZE": 20,
    "GROUP_FILTER_MODE": "blacklist",
    "GROUP_BLACKLIST": [],
    "GROUP_WHITELIST": [],
    "PRIVATE_CHAT_ENABLED": True
}
CONFIG_FILE = "config.json"

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            d = DEFAULT_CONFIG.copy()
            d.update(cfg)
            return d
        except Exception as e:
            print("读取 config.json 失败，使用默认：", e)
            return DEFAULT_CONFIG.copy()
    else:
        return DEFAULT_CONFIG.copy()

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        print("✓ 配置已保存到 config.json")
        return True
    except Exception as e:
        print("保存配置失败：", e)
        return False

CONFIG = load_config()
if not os.path.exists(CONFIG_FILE):
    save_config(CONFIG)

app = Flask(__name__)
stop_event = threading.Event()
ws_app = None
ws_app_lock = threading.Lock()
db_conn = None
db_lock = threading.Lock()
scheduler_thread = None
scheduler_thread_lock = threading.Lock()

DB_INIT_SQL = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id TEXT,
    post_type TEXT,
    message_type TEXT,
    user_id TEXT,
    group_id TEXT,
    group_name TEXT,
    sender_name TEXT,
    content TEXT,
    raw_json TEXT,
    processed INTEGER DEFAULT 0,
    ai_request_id TEXT,
    received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ai_responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    messages_ids TEXT,
    ai_response TEXT,
    ai_created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    email_sent INTEGER DEFAULT 0,
    email_sent_at TIMESTAMP
);
"""

def init_db(db_file):
    global db_conn
    conn = sqlite3.connect(db_file, check_same_thread=False)
    cur = conn.cursor()
    cur.executescript(DB_INIT_SQL)
    conn.commit()
    return conn

db_conn = init_db(CONFIG["DB_FILE"])

def should_process_message(message_type, group_id, user_id):
    """根据配置决定是否处理这条消息"""
    
    # 私聊消息
    if message_type == "private":
        return CONFIG.get("PRIVATE_CHAT_ENABLED", True)
    
    # 群消息
    if message_type == "group":
        group_id = str(group_id)
        mode = CONFIG.get("GROUP_FILTER_MODE", "blacklist")
        
        if mode == "blacklist":
            # 黑名单模式：黑名单中的群被忽略
            blacklist = CONFIG.get("GROUP_BLACKLIST", [])
            return group_id not in [str(g) for g in blacklist]
        elif mode == "whitelist":
            # 白名单模式：只处理白名单中的群
            whitelist = CONFIG.get("GROUP_WHITELIST", [])
            return group_id in [str(g) for g in whitelist]
    
    return True

def parse_cq_codes(text):
    """解析 CQ 码，将特殊消息转换为可读格式"""
    if not text:
        return text
    
    import re
    
    # [CQ:face,id=XXX] → 表情(id=XXX)
    text = re.sub(r'\[CQ:face,id=(\d+)(?:,raw=[^\]]*)?]', r'[表情:\1]', text)
    
    # [CQ:image,file=XXX] → 图片
    text = re.sub(r'\[CQ:image,file=([^\]]*)]', r'[图片:\1]', text)
    
    # [CQ:at,qq=XXX] → @用户
    text = re.sub(r'\[CQ:at,qq=(\d+)]', r'[@:\1]', text)
    
    # [CQ:record,file=XXX] → 语音
    text = re.sub(r'\[CQ:record,file=([^\]]*)]', r'[语音:\1]', text)
    
    # [CQ:video,file=XXX] → 视频
    text = re.sub(r'\[CQ:video,file=([^\]]*)]', r'[视频:\1]', text)
    
    # [CQ:json,data=XXX] → JSON
    text = re.sub(r'\[CQ:json,data=([^\]]*)]', r'[JSON卡片]', text)
    
    # 清理 raw 参数中的 &#91;object Object&#93; 等编码
    text = text.replace('&#91;object Object&#93;', '')
    text = text.replace('&#93;', ']')
    text = text.replace('&#91;', '[')
    
    return text


def get_group_name(group_id):
    """获取群名称：优先调用 NapCat API"""
    if not group_id:
        return ""
    try:
        api_url = CONFIG.get("API_BASE_URL", "http://127.0.0.1:3000")
        url = f"{api_url}/get_group_info?group_id={group_id}"
        r = requests.get(url, timeout=3)
        data = r.json()
        if isinstance(data, dict):
            if data.get("status") == "ok" or data.get("retcode") == 0:
                info = data.get("data", data)
                return info.get("group_name") or info.get("groupName") or ""
    except Exception:
        pass
    
    with db_lock:
        cur = db_conn.cursor()
        try:
            cur.execute("SELECT group_name FROM messages WHERE group_id=? AND group_name IS NOT NULL AND group_name!='' ORDER BY id DESC LIMIT 1", (str(group_id),))
            row = cur.fetchone()
            if row:
                return row[0]
        except:
            pass
    return ""

def save_message_to_db(post):
    try:
        msg_id = post.get("message_id") or post.get("messageId") or ""
        post_type = post.get("post_type") or post.get("postType") or ""
        message_type = post.get("message_type") or post.get("messageType") or ""
        user_id = ""
        group_id = ""
        group_name = ""
        sender_name = ""
        
        if message_type == "private":
            user_id = str(post.get("user_id") or post.get("userId") or post.get("sender", {}).get("user_id") or "")
            sender = post.get("sender", {})
            sender_name = sender.get("nickname") or sender.get("card") or sender.get("username") or ""
        elif message_type == "group":
            group_id = str(post.get("group_id") or post.get("groupId") or "")
            group_name = post.get("group_name") or post.get("groupName") or ""
            sender = post.get("sender", {})
            sender_name = sender.get("nickname") or sender.get("card") or sender.get("username") or ""
            user_id = str(sender.get("user_id") or post.get("user_id") or "")
        else:
            user_id = str(post.get("sender", {}).get("user_id") or "")
            sender_name = post.get("sender", {}).get("nickname") or ""

        content = ""
        if isinstance(post.get("raw_message", None), str):
            content = post.get("raw_message")
        elif isinstance(post.get("message", None), str):
            content = post.get("message")
        elif "message_chain" in post or "messageChain" in post:
            arr = post.get("message_chain") or post.get("messageChain") or []
            if isinstance(arr, list):
                try:
                    content = " ".join((seg.get("text") or seg.get("content") or str(seg)) for seg in arr)
                except:
                    content = str(arr)
            else:
                content = str(arr)
        else:
            try:
                content = post.get("raw_message") or post.get("message") or json.dumps(post, ensure_ascii=False)
            except:
                content = str(post)
        
        # 解析 CQ 码
        content = parse_cq_codes(content)

        if not group_name and group_id:
            group_name = get_group_name(group_id) or ""

        with db_lock:
            cur = db_conn.cursor()
            cur.execute(
                "INSERT INTO messages (msg_id, post_type, message_type, user_id, group_id, group_name, sender_name, content, raw_json, processed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (msg_id, post_type, message_type, user_id, group_id, group_name, sender_name, content, json.dumps(post, ensure_ascii=False))
            )
            db_conn.commit()
            return cur.lastrowid
    except Exception:
        print("保存消息失败：", traceback.format_exc())
        return None

def on_message(ws, message):
    try:
        data = json.loads(message) if isinstance(message, str) else message
    except:
        return
    post_type = data.get("post_type") or data.get("postType") or ""
    if post_type == "message":
        message_type = data.get("message_type") or data.get("messageType") or ""
        if message_type in ("private", "group"):
            group_id = data.get("group_id") or data.get("groupId") or ""
            user_id = data.get("user_id") or data.get("userId") or ""
            
            # 检查是否应该处理这条消息
            if should_process_message(message_type, group_id, user_id):
                rowid = save_message_to_db(data)
                print(f"[{datetime.now()}] 消息 id={rowid} type={message_type}")
            else:
                if message_type == "group":
                    print(f"[{datetime.now()}] 消息已过滤 - 群:{group_id}")
                else:
                    print(f"[{datetime.now()}] 消息已过滤 - 私聊")

def on_error(ws, error):
    print("WebSocket 错误：", error)

def on_close(ws, code, reason):
    print("WebSocket 关闭")

def on_open(ws):
    print("WebSocket 已连接到 NapCat")

def start_ws_client_in_thread(stop_evt):
    def _run():
        global ws_app
        ws_url = CONFIG.get("WS_URL")
        while not stop_evt.is_set():
            try:
                with ws_app_lock:
                    ws_app = websocket.WebSocketApp(ws_url, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
                ws_app.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                print(f"WebSocket 异常，5秒后重试：{e}")
                time.sleep(5)
            if stop_evt.is_set():
                break
            time.sleep(1)
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t

def stop_ws_client():
    global ws_app
    with ws_app_lock:
        if ws_app:
            try:
                ws_app.close()
            except:
                pass
            ws_app = None

def fetch_unprocessed_messages(limit=None):
    with db_lock:
        cur = db_conn.cursor()
        q = "SELECT id, msg_id, post_type, message_type, user_id, group_id, group_name, sender_name, content, raw_json, received_at FROM messages WHERE processed = 0 ORDER BY received_at ASC"
        if limit:
            q += " LIMIT ?"
            cur.execute(q, (limit,))
        else:
            cur.execute(q)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in rows]

def mark_messages_processed(ids):
    if not ids:
        return
    with db_lock:
        cur = db_conn.cursor()
        placeholders = ",".join("?" * len(ids))
        cur.execute(f"UPDATE messages SET processed = 1 WHERE id IN ({placeholders})", ids)
        db_conn.commit()

def save_ai_response(message_ids, ai_json_str, email_sent=False):
    with db_lock:
        cur = db_conn.cursor()
        cur.execute("INSERT INTO ai_responses (messages_ids, ai_response, email_sent) VALUES (?, ?, ?)", 
                   (",".join(str(i) for i in message_ids), ai_json_str, 1 if email_sent else 0))
        db_conn.commit()

AI_PROMPT_TEMPLATE = """你是信息浓缩专家。下面给你的是若干条 QQ 聊天消息，格式：
[时间] 场景 - 发送者 说：消息原文

你的任务：
1. 按发送者/群分组
2. 提炼核心信息
3. 标注优先级（high/medium/low）
4. 去重并按优先级排序

**必须返回 JSON 格式，不要返回其他内容**：
{{
  "summary": {{
    "total_messages": 数字,
    "time_range": "开始时间 ~ 结束时间",
    "groups": [
      {{
        "name": "群名或私聊对象名",
        "type": "group 或 private",
        "messages": [
          {{
            "priority": "high|medium|low",
            "content": "摘要内容",
            "sender": "发送者"
          }}
        ]
      }}
    ]
  }}
}}

现在开始处理，输出 JSON：

{messages}"""

def build_ai_payload(messages_for_ai):
    formatted_msgs = ""
    for m in messages_for_ai:
        timestamp = m.get("received_at") or ""
        if m.get("group_id"):
            scene = f"群[{m.get('group_name') or m.get('group_id')}]"
        else:
            scene = "私聊"
        sender = m.get("sender_name") or m.get("user_id") or "unknown"
        content = m.get("content") or ""
        formatted_msgs += f"[{timestamp}] {scene} - {sender} 说：{content}\n"
    
    prompt = AI_PROMPT_TEMPLATE.format(messages=formatted_msgs)
    payload = {
        "model": CONFIG.get("AI_MODEL"),
        "messages": [
            {"role": "system", "content": "你是专业的消息管理助手，必须返回有效的 JSON 格式，不要有任何额外文本。"},
            {"role": "user", "content": prompt}
        ]
    }
    return payload

def call_ai_api(payload):
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {CONFIG.get('AI_API_KEY')}"}
    try:
        r = requests.post(CONFIG.get("AI_API_URL"), headers=headers, json=payload, timeout=60)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("AI 接口失败：", e)
        return None

def extract_ai_json(ai_raw):
    """从 AI 响应中提取 JSON"""
    try:
        if isinstance(ai_raw, dict) and "choices" in ai_raw:
            for choice in ai_raw.get("choices", []):
                msg = choice.get("message", {})
                text = msg.get("content", "")
                if text:
                    import re
                    json_match = re.search(r'\{.*\}', text, re.DOTALL)
                    if json_match:
                        json_str = json_match.group(0)
                        return json.loads(json_str)
    except Exception as e:
        print("JSON 解析失败：", e)
    return None

def generate_email_html(ai_data, messages_list):
    """根据 AI 返回的 JSON 生成邮件 HTML"""
    if not ai_data:
        ai_data = {"summary": {"total_messages": len(messages_list), "time_range": "未知", "groups": []}}
    
    summary = ai_data.get("summary", {})
    groups = summary.get("groups", [])
    
    html_parts = []
    
    html_parts.append('''<div style="background: linear-gradient(135deg, #FF6B9D 0%, #FF8FB3 100%); padding: 24px; border-radius: 12px 12px 0 0; text-align: center; color: white;">
      <h1 style="margin: 0; font-size: 28px; font-weight: 700;">💌 QQ 消息摘要</h1>
      <p style="margin: 12px 0 0 0; font-size: 14px; opacity: 0.95;">''' + summary.get("time_range", "消息汇总") + '''</p>
      <p style="margin: 6px 0 0 0; font-size: 13px; opacity: 0.9;">共 ''' + str(summary.get("total_messages", 0)) + ''' 条消息</p>
    </div>''')
    
    html_parts.append('<div style="background: #FAFAFA; padding: 28px; color: #2C3E50; font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, sans-serif; line-height: 1.8;">')
    
    for idx, group in enumerate(groups):
        group_name = group.get("name", "未知")
        group_type = group.get("type", "group")
        group_msgs = group.get("messages", [])
        
        is_group = group_type == "group"
        icon = "👥" if is_group else "💬"
        border_color = "#FF6B9D" if is_group else "#FF8FB3"
        bg_color = "#FFF5F9" if is_group else "#FFF9FB"
        
        html_parts.append(f'''<details style="margin-bottom: 20px; background: {bg_color}; border-left: 4px solid {border_color}; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(255,107,157,0.08);">
          <summary style="cursor: pointer; padding: 16px; font-weight: 600; color: {border_color}; user-select: none; display: flex; justify-content: space-between; align-items: center;">
            <span>{icon} {group_name} <span style="font-size: 12px; color: #999; margin-left: 8px;">({len(group_msgs)} 条)</span></span>
            <span style="font-size: 12px; color: #999;">▼</span>
          </summary>
          <div style="padding: 16px; border-top: 1px solid {border_color}20;">
            <ul style="margin: 0; padding-left: 0; list-style: none;">''')
        
        for msg in group_msgs:
            priority = msg.get("priority", "medium")
            priority_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(priority, "•")
            content = msg.get("content", "").replace("<", "&lt;").replace(">", "&gt;")
            sender = msg.get("sender", "")
            
            html_parts.append(f'''<li style="margin-bottom: 12px; padding: 12px; background: white; border-radius: 6px; border: 1px solid #FFE4ED; display: flex; gap: 12px;">
              <span style="font-size: 16px; flex-shrink: 0;">{priority_icon}</span>
              <div style="flex: 1;">
                <div style="font-weight: 600; color: #FF6B9D; font-size: 13px; margin-bottom: 4px;">{sender}</div>
                <div style="color: #2C3E50; word-break: break-word;">{content}</div>
              </div>
            </li>''')
        
        html_parts.append('''</ul></div></details>''')
    
    html_parts.append('</div>')
    
    html_parts.append('''<div style="background: #F5F7FA; padding: 20px; border-top: 2px solid #FFE4ED;">
      <details style="cursor: pointer;">
        <summary style="font-weight: 600; color: #FF6B9D; font-size: 15px; user-select: none;">📂 原始消息详情（点击展开）</summary>
        <div style="margin-top: 16px; border-radius: 8px; overflow: hidden; background: white; border: 1px solid #E8EEF5;">''')
    
    for m in messages_list:
        mid = m.get("id")
        ts = m.get("received_at") or ""
        scene = (f"群: {m.get('group_name') or m.get('group_id')}") if m.get("group_id") else (f"私聊: {m.get('sender_name') or m.get('user_id') or ''}")
        content = (m.get("content") or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        
        html_parts.append(f'''<div id="msg{mid}" style="padding: 12px 16px; border-bottom: 1px solid #E8EEF5; background: #FAFBFC;">
          <div style="color: #FF8FB3; font-size: 12px; margin-bottom: 6px; font-weight: 600;">#{mid} · {ts} · {scene}</div>
          <div style="color: #2C3E50; font-size: 14px; word-break: break-word;">{content}</div>
        </div>''')
    
    html_parts.append('</div></details></div>')
    
    html_parts.append('''<div style="background: #F5F7FA; padding: 16px; border-top: 1px solid #E8EEF5; text-align: center; color: #7F8FA3; font-size: 12px;">
      ✨ 本邮件由 NapCat AI 助手自动生成
    </div>''')
    
    body_html = '<div style="max-width: 720px; margin: 0 auto; background: white; border-radius: 12px; overflow: hidden; box-shadow: 0 10px 40px rgba(0,0,0,0.1);">' + "\n".join(html_parts) + '</div>'
    return body_html

def send_email(subject, body_html):
    msg = MIMEMultipart("alternative")
    sender_name = CONFIG.get("EMAIL_SENDER_NAME") or ""
    from_addr = CONFIG.get("EMAIL_FROM")
    if sender_name:
        msg["From"] = f"{sender_name} <{from_addr}>"
    else:
        msg["From"] = from_addr
    msg["To"] = CONFIG.get("EMAIL_TO")
    msg["Subject"] = subject
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    try:
        smtp = smtplib.SMTP(CONFIG.get("SMTP_HOST"), CONFIG.get("SMTP_PORT"))
        smtp.ehlo()
        smtp.starttls()
        smtp.login(CONFIG.get("SMTP_USER"), CONFIG.get("SMTP_PASSWORD"))
        smtp.sendmail(from_addr, [CONFIG.get("EMAIL_TO")], msg.as_string())
        smtp.quit()
        return True
    except Exception as e:
        print("发送邮件失败：", e)
        return False

last_notification = {"status": "", "message": "", "timestamp": None}

def process_and_send_batch(triggered_by="scheduler"):
    global last_notification
    print(f"[{datetime.now()}] 触发（{triggered_by}） - 处理消息...")
    msgs = fetch_unprocessed_messages(limit=CONFIG.get("BATCH_MAX_MESSAGES"))
    if not msgs:
        result = {"status": "no_messages", "message": "没有未处理消息"}
        last_notification = {**result, "timestamp": datetime.now().isoformat()}
        return result

    print(f"准备处理 {len(msgs)} 条消息...")
    payload = build_ai_payload(msgs)
    ai_raw = call_ai_api(payload)
    if not ai_raw:
        result = {"status": "ai_failed", "message": "AI 接口失败"}
        last_notification = {**result, "timestamp": datetime.now().isoformat()}
        return result

    ai_data = extract_ai_json(ai_raw)
    if not ai_data:
        print("未能解析 AI JSON，使用默认格式")
        ai_data = None
    
    ai_json_str = json.dumps(ai_data, ensure_ascii=False) if ai_data else "{}"
    
    try:
        times = [m.get("received_at") for m in msgs if m.get("received_at")]
        if times:
            tmin = min(times)
            tmax = max(times)
            subject = f"📬 QQ 摘要 - {tmin[:16]} ~ {tmax[:16]}"
        else:
            subject = f"📬 QQ 摘要 - {datetime.now().strftime('%Y-%m-%d')}"
    except:
        subject = f"📬 QQ 摘要 - {datetime.now().strftime('%Y-%m-%d')}"
    
    body_html = generate_email_html(ai_data, msgs)
    sent = send_email(subject, body_html)
    
    if sent:
        print("邮件发送成功")
        ids = [m.get("id") for m in msgs]
        save_ai_response(ids, ai_json_str, email_sent=True)
        mark_messages_processed(ids)
        result = {"status": "ok", "count": len(ids), "message": f"✅ 成功处理 {len(ids)} 条消息并发送邮件"}
        last_notification = {**result, "timestamp": datetime.now().isoformat()}
        return result
    else:
        result = {"status": "email_failed", "message": "邮件发送失败"}
        last_notification = {**result, "timestamp": datetime.now().isoformat()}
        return result

def schedule_jobs():
    schedule.clear()
    times = CONFIG.get("RUN_TIMES", []) or []
    last_trigger_times = {}  # 记录上次触发时间，防止重复
    
    for t in times:
        try:
            hh_mm = t.strip()
            if ":" not in hh_mm:
                continue
            hh, mm = hh_mm.split(":")
            hh = int(hh)
            mm = int(mm)
            time_key = f"{hh:02d}:{mm:02d}"
            last_trigger_times[time_key] = None
            
            def make_job(tb, tk):
                def job():
                    now = datetime.now()
                    current_time = now.strftime("%H:%M")
                    
                    # 如果在同一分钟内已经执行过，则跳过
                    if last_trigger_times[tk] == current_time:
                        return
                    
                    last_trigger_times[tk] = current_time
                    print(f"\n{'='*60}")
                    print(f"[{now}] 触发定时任务（{tb}）")
                    print(f"{'='*60}")
                    process_and_send_batch(triggered_by=f"schedule {tb}")
                
                return job
            
            schedule.every().day.at(f"{hh:02d}:{mm:02d}").do(make_job(t, time_key))
            print(f"✓ 已安排每天 {hh:02d}:{mm:02d}")
        except Exception as e:
            print(f"RUN_TIMES 解析失败：{t} {e}")
    
    def run_loop():
        print("✓ Scheduler 启动")
        while not stop_event.is_set():
            try:
                schedule.run_pending()
            except Exception as e:
                print(f"Scheduler 错误：{e}")
            time.sleep(1)
    
    global scheduler_thread
    with scheduler_thread_lock:
        if scheduler_thread and scheduler_thread.is_alive():
            return  # 如果线程已经运行，不要重新启动
        scheduler_thread = threading.Thread(target=run_loop, daemon=True)
        scheduler_thread.start()

# ========== Flask 模板 ==========
INDEX_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
  <title>NapCat AI 助手</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding: 16px; }
    .container { max-width: 900px; margin: 0 auto; }
    @media (max-width: 768px) {
      body { padding: 12px; }
      .container { max-width: 100%; }
      .form-grid { grid-template-columns: 1fr !important; }
      .button-group { flex-direction: column; }
      .btn { width: 100%; }
      .header { flex-direction: column; gap: 12px; }
      .nav-buttons { width: 100%; justify-content: space-between; }
      table { font-size: 12px; }
      th, td { padding: 8px 6px; }
      .card { padding: 16px; margin-bottom: 16px; }
      .card h2 { font-size: 16px; }
      .header h1 { font-size: 24px; }
    }
    .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 32px; background: white; padding: 24px; border-radius: 16px; box-shadow: 0 10px 40px rgba(0,0,0,0.1); }
    .header h1 { font-size: 28px; background: linear-gradient(135deg, #FF6B9D, #FF8FB3); -webkit-background-clip: text; -webkit-text-fill-color: transparent; font-weight: 700; }
    .nav-buttons { display: flex; gap: 8px; }
    .nav-buttons a, .nav-buttons button { padding: 8px 16px; border-radius: 8px; text-decoration: none; cursor: pointer; border: none; font-size: 14px; font-weight: 500; transition: all 0.3s; }
    .nav-buttons .active { background: linear-gradient(135deg, #FF6B9D, #FF8FB3); color: white; }
    .nav-buttons a { background: rgba(255,107,157,0.1); color: #FF6B9D; }
    .nav-buttons a:hover { transform: translateY(-2px); }
    .card { background: white; border-radius: 16px; padding: 28px; margin-bottom: 24px; box-shadow: 0 10px 40px rgba(0,0,0,0.08); }
    .card h2 { font-size: 18px; color: #FF6B9D; margin-bottom: 20px; padding-bottom: 12px; border-bottom: 2px solid #FF8FB3; font-weight: 700; }
    .form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 20px; margin-bottom: 20px; }
    .form-group { display: flex; flex-direction: column; }
    .form-group label { font-size: 13px; font-weight: 600; color: #2C3E50; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.5px; }
    .form-group { position: relative; }
    .form-group input { padding: 12px 14px; border: 2px solid #E8EEF5; border-radius: 8px; font-size: 14px; transition: all 0.3s; background: #FAFBFC; }
    .form-group input:focus { outline: none; border-color: #FF8FB3; background: white; box-shadow: 0 0 0 3px rgba(255,107,157,0.1); }
    .password-toggle { position: absolute; right: 14px; top: 43px; cursor: pointer; color: #FF8FB3; font-size: 14px; padding: 4px 8px; user-select: none; transition: all 0.3s; font-weight: 600; line-height: 1; }
    .password-toggle:hover { color: #FF6B9D; }
    .button-group { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 24px; padding-top: 24px; border-top: 2px solid #E8EEF5; }
    .btn { padding: 12px 24px; border-radius: 8px; border: none; font-size: 14px; font-weight: 600; cursor: pointer; transition: all 0.3s; text-decoration: none; display: inline-block; text-align: center; }
    .btn-primary { background: linear-gradient(135deg, #FF6B9D, #FF8FB3); color: white; flex: 1; min-width: 140px; }
    .btn-primary:hover { transform: translateY(-3px); box-shadow: 0 12px 30px rgba(255,107,157,0.4); }
    .btn-primary:active { transform: translateY(-1px); }
    .btn-secondary { background: rgba(255,107,157,0.1); color: #FF6B9D; border: 2px solid #FF6B9D; }
    .btn-secondary:hover { background: rgba(255,107,157,0.15); transform: translateY(-2px); }
    .btn-danger { background: #FF6B6B; color: white; }
    .btn-danger:hover { background: #FF5252; transform: translateY(-2px); }
    .notification { position: fixed; top: 20px; right: 20px; background: white; padding: 16px 24px; border-radius: 12px; box-shadow: 0 10px 40px rgba(0,0,0,0.2); border-left: 4px solid #FF6B9D; animation: slideIn 0.3s ease; z-index: 1000; max-width: 400px; word-wrap: break-word; }
    .notification.success { border-left-color: #2ECC71; background: #F0FFF4; }
    .notification.error { border-left-color: #FF6B6B; background: #FFF5F5; }
    .notification.info { border-left-color: #3498DB; background: #EBF8FF; }
    @keyframes slideIn { from { transform: translateX(400px); opacity: 0; } to { transform: translateX(0); opacity: 1; } }
    @keyframes slideOut { from { transform: translateX(0); opacity: 1; } to { transform: translateX(400px); opacity: 0; } }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>🐾 NapCat AI 助手</h1>
      <div class="nav-buttons">
        <button class="active">⚙️ 配置</button>
        <a href="/db/messages">📊 数据库</a>
        <a href="/db/ai">🤖 摘要</a>
      </div>
    </div>

    <div id="notificationArea"></div>

    <div class="card" style="background: linear-gradient(135deg, rgba(255,107,157,0.05), rgba(255,143,179,0.05)); border-left: 4px solid #FF8FB3;">
      <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 16px;">
        <div style="padding: 12px; background: white; border-radius: 8px; text-align: center;">
          <div style="font-size: 24px; font-weight: 700; color: #FF6B9D;" id="unprocessedCount">-</div>
          <div style="font-size: 12px; color: #7F8FA3; margin-top: 4px;">待处理消息</div>
        </div>
        <div style="padding: 12px; background: white; border-radius: 8px; text-align: center;">
          <div style="font-size: 24px; font-weight: 700; color: #FF6B9D;" id="totalCount">-</div>
          <div style="font-size: 12px; color: #7F8FA3; margin-top: 4px;">消息总数</div>
        </div>
        <div style="padding: 12px; background: white; border-radius: 8px; text-align: center;">
          <div style="font-size: 24px; font-weight: 700; color: #FF6B9D;" id="emailCount">-</div>
          <div style="font-size: 12px; color: #7F8FA3; margin-top: 4px;">已发送邮件</div>
        </div>
        <div style="padding: 12px; background: white; border-radius: 8px; text-align: center;">
          <div style="font-size: 24px; font-weight: 700; color: #2ECC71;" id="wsStatus">✗</div>
          <div style="font-size: 12px; color: #7F8FA3; margin-top: 4px;">WebSocket</div>
        </div>
      </div>
    </div>

    <form id="configForm">
      <div class="card">
        <h2>🌐 基础设置</h2>
        <div class="form-grid">
          <div class="form-group">
            <label>NapCat WebSocket</label>
            <input type="text" name="WS_URL" value="{{cfg['WS_URL']}}">
          </div>
          <div class="form-group">
            <label>NapCat HTTP API</label>
            <input type="text" name="API_BASE_URL" value="{{cfg.get('API_BASE_URL', 'http://127.0.0.1:3000')}}">
          </div>
          <div class="form-group">
            <label>数据库文件</label>
            <input type="text" name="DB_FILE" value="{{cfg['DB_FILE']}}">
          </div>
          <div class="form-group">
            <label>定时任务 (HH:MM,HH:MM)</label>
            <input type="text" name="RUN_TIMES" value="{{cfg['RUN_TIMES']|join(',')}}">
          </div>
          <div class="form-group">
            <label>单批最大消息数</label>
            <input type="number" name="BATCH_MAX_MESSAGES" value="{{cfg['BATCH_MAX_MESSAGES']}}">
          </div>
          <div class="form-group">
            <label>每页显示条数</label>
            <input type="number" name="PAGE_SIZE" value="{{cfg['PAGE_SIZE']}}">
          </div>
        </div>
      </div>

      <div class="card">
        <h2>📧 邮件 & SMTP</h2>
        <div class="form-grid">
          <div class="form-group">
            <label>SMTP 主机</label>
            <input type="text" name="SMTP_HOST" value="{{cfg['SMTP_HOST']}}">
          </div>
          <div class="form-group">
            <label>SMTP 端口</label>
            <input type="number" name="SMTP_PORT" value="{{cfg['SMTP_PORT']}}">
          </div>
          <div class="form-group">
            <label>SMTP 用户</label>
            <input type="text" name="SMTP_USER" value="{{cfg['SMTP_USER']}}">
          </div>
          <div class="form-group">
            <label>SMTP 密码</label>
            <input type="password" name="SMTP_PASSWORD" id="SMTP_PASSWORD" value="{{cfg['SMTP_PASSWORD']}}">
            <span class="password-toggle" onclick="togglePassword('SMTP_PASSWORD')">显示</span>
          </div>
          <div class="form-group">
            <label>发件邮箱</label>
            <input type="email" name="EMAIL_FROM" value="{{cfg['EMAIL_FROM']}}">
          </div>
          <div class="form-group">
            <label>发件人昵称</label>
            <input type="text" name="EMAIL_SENDER_NAME" value="{{cfg['EMAIL_SENDER_NAME']}}">
          </div>
          <div class="form-group">
            <label>收件邮箱</label>
            <input type="email" name="EMAIL_TO" value="{{cfg['EMAIL_TO']}}">
          </div>
        </div>
      </div>

      <div class="card">
        <h2>🎯 群组过滤管理</h2>
        <div style="margin-bottom: 20px;">
          <label style="font-size: 13px; font-weight: 600; color: #2C3E50; display: block; margin-bottom: 12px;">
            <input type="radio" name="filterMode" value="blacklist" onchange="updateFilterMode('blacklist')" style="margin-right: 8px;">
            黑名单模式 (排除选中的群)
          </label>
          <label style="font-size: 13px; font-weight: 600; color: #2C3E50; display: block; margin-bottom: 12px;">
            <input type="radio" name="filterMode" value="whitelist" onchange="updateFilterMode('whitelist')" style="margin-right: 8px;">
            白名单模式 (仅处理选中的群)
          </label>
          <label style="font-size: 13px; font-weight: 600; color: #2C3E50; display: flex; align-items: center; margin-bottom: 12px;">
            <input type="checkbox" id="privateEnabled" onchange="updateGroupFilter()" style="margin-right: 8px;">
            包含私聊消息
          </label>
        </div>
        
        <div style="border: 2px solid #FFE4ED; border-radius: 8px; padding: 16px; max-height: 300px; overflow-y: auto;">
          <div id="groupList" style="display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px;">
            <div style="text-align: center; color: #999; padding: 20px; grid-column: 1/-1;">加载中...</div>
          </div>
        </div>
      </div>
        <div class="form-grid">
          <div class="form-group">
            <label>AI API 地址</label>
            <input type="text" name="AI_API_URL" value="{{cfg['AI_API_URL']}}">
          </div>
          <div class="form-group">
            <label>AI API Key</label>
            <input type="password" name="AI_API_KEY" id="AI_API_KEY" value="{{cfg['AI_API_KEY']}}">
            <span class="password-toggle" onclick="togglePassword('AI_API_KEY')">显示</span>
          </div>
          <div class="form-group">
            <label>AI 模型</label>
            <input type="text" name="AI_MODEL" value="{{cfg['AI_MODEL']}}">
          </div>
        </div>
      </div>

      <div class="button-group">
        <button type="button" class="btn btn-primary" onclick="saveConfig()">💾 保存配置</button>
        <button type="button" class="btn btn-secondary" onclick="triggerManual()">⚡ 手动触发汇总</button>
        <a href="/db/messages" class="btn btn-secondary">📊 消息数据库</a>
        <a href="/db/ai" class="btn btn-secondary">📋 摘要记录</a>
        <button type="button" class="btn btn-danger" onclick="shutdown()">⛔ 关闭程序</button>
      </div>
    </form>
  </div>

  <div id="previewModal" style="display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5); z-index: 2000; align-items: center; justify-content: center;">
    <div style="background: white; border-radius: 16px; max-width: 90%; max-height: 90vh; overflow-y: auto; box-shadow: 0 20px 60px rgba(0,0,0,0.3); display: flex; flex-direction: column;">
      <div style="display: flex; justify-content: space-between; align-items: center; padding: 24px; border-bottom: 2px solid #FF8FB3; sticky; top: 0; background: white;">
        <h3 style="font-size: 18px; color: #FF6B9D; margin: 0;">📧 邮件预览</h3>
        <div style="display: flex; gap: 12px;">
          <button onclick="sendPreviewEmail()" style="padding: 8px 16px; background: #2ECC71; color: white; border: none; border-radius: 8px; cursor: pointer; font-weight: 600;">✉️ 发送此邮件</button>
          <button onclick="closePreview()" style="padding: 8px 16px; background: #FF6B6B; color: white; border: none; border-radius: 8px; cursor: pointer; font-weight: 600;">关闭</button>
        </div>
      </div>
      <div style="padding: 24px; flex: 1;">
        <div style="margin-bottom: 16px; padding: 12px; background: #F0FFF4; border-left: 4px solid #2ECC71; border-radius: 8px;">
          <strong style="color: #2ECC71;">主题：</strong> <span id="previewSubject"></span>
          <div style="font-size: 12px; color: #999; margin-top: 4px;">包含 <span id="previewCount">0</span> 条消息</div>
        </div>
        <div id="previewContent" style="border: 1px solid #E8EEF5; border-radius: 8px; padding: 16px; background: #FAFBFC;"></div>
      </div>
    </div>
  </div>

  <script>
    let lastNotificationTime = null;
    let currentFilterMode = "blacklist";
    let allGroups = [];

    // 初始化：加载群组列表
    async function loadGroups() {
      try {
        const res = await fetch('/api/get_groups');
        const data = await res.json();
        allGroups = data.groups;
        currentFilterMode = data.filter_mode;
        
        document.querySelector(`input[name="filterMode"][value="${currentFilterMode}"]`).checked = true;
        document.getElementById('privateEnabled').checked = data.private_enabled;
        
        renderGroupList(data.groups, data.blacklist, data.whitelist, currentFilterMode);
      } catch (err) {
        console.error('加载群组失败', err);
      }
    }

    function renderGroupList(groups, blacklist, whitelist, mode) {
      const container = document.getElementById('groupList');
      if (groups.length === 0) {
        container.innerHTML = '<div style="text-align: center; color: #999; padding: 20px; grid-column: 1/-1;">暂无群组</div>';
        return;
      }
      
      let filterList = mode === "blacklist" ? blacklist : whitelist;
      
      container.innerHTML = groups.map(g => {
        const isChecked = filterList.includes(String(g.id));
        const status = mode === "blacklist" ? (isChecked ? "🚫 已忽略" : "✓ 已启用") : (isChecked ? "✓ 已启用" : "✗ 已禁用");
        const statusColor = (isChecked && mode === "blacklist") || (!isChecked && mode === "whitelist") ? "#FF6B6B" : "#2ECC71";
        
        return `<div style="background: white; border: 1px solid #FFE4ED; border-radius: 8px; padding: 12px;">
          <label style="display: flex; align-items: center; gap: 8px; cursor: pointer;">
            <input type="checkbox" class="group-checkbox" data-id="${g.id}" ${isChecked ? 'checked' : ''} onchange="updateGroupFilter()">
            <div style="flex: 1; min-width: 0;">
              <div style="font-weight: 600; color: #2C3E50; font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">${g.name}</div>
              <div style="font-size: 11px; color: #999;">ID: ${g.id}</div>
            </div>
          </label>
          <div style="font-size: 12px; color: ${statusColor}; margin-top: 6px; font-weight: 600;">${status}</div>
        </div>`;
      }).join('');
    }

    function updateFilterMode(mode) {
      currentFilterMode = mode;
      updateGroupFilter();
    }

    async function updateGroupFilter() {
      const checkboxes = document.querySelectorAll('.group-checkbox');
      const checked = Array.from(checkboxes)
        .filter(cb => cb.checked)
        .map(cb => cb.dataset.id);
      
      const privateEnabled = document.getElementById('privateEnabled').checked;
      
      const payload = {
        mode: currentFilterMode,
        blacklist: currentFilterMode === "blacklist" ? checked : [],
        whitelist: currentFilterMode === "whitelist" ? checked : [],
        private_enabled: privateEnabled
      };
      
      try {
        const res = await fetch('/api/update_group_filter', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (data.ok) {
          showNotification('✅ ' + data.message, 'success');
          renderGroupList(allGroups, payload.blacklist, payload.whitelist, currentFilterMode);
        }
      } catch (err) {
        showNotification('❌ 更新失败', 'error');
      }
    }

    // 页面加载时初始化
    window.addEventListener('load', loadGroups);

    // 定时轮询系统通知和状态（每3秒检查一次）
    setInterval(async () => {
      try {
        // 获取通知
        const resNotif = await fetch('/api/last_notification');
        const dataNotif = await resNotif.json();
        
        if (dataNotif.timestamp && dataNotif.timestamp !== lastNotificationTime) {
          lastNotificationTime = dataNotif.timestamp;
          let notifType = 'info';
          if (dataNotif.status === 'ok') notifType = 'success';
          else if (dataNotif.status === 'no_messages') notifType = 'info';
          else notifType = 'error';
          showNotification(dataNotif.message || dataNotif.status, notifType);
        }

        // 获取系统状态
        const resStatus = await fetch('/api/system_status');
        const dataStatus = await resStatus.json();
        document.getElementById('unprocessedCount').textContent = dataStatus.unprocessed_messages;
        document.getElementById('totalCount').textContent = dataStatus.total_messages;
        document.getElementById('emailCount').textContent = dataStatus.sent_emails;
        document.getElementById('wsStatus').textContent = dataStatus.ws_connected ? '✓' : '✗';
        document.getElementById('wsStatus').style.color = dataStatus.ws_connected ? '#2ECC71' : '#FF6B6B';
      } catch (err) {
        // 轮询失败时不显示错误
      }
    }, 3000);

    function togglePassword(inputId) {
      const input = document.getElementById(inputId);
      const toggle = event.target;
      if (input.type === 'password') {
        input.type = 'text';
        toggle.textContent = '隐藏';
      } else {
        input.type = 'password';
        toggle.textContent = '显示';
      }
    }

    function showNotification(message, type='info', duration=3500) {
      const area = document.getElementById('notificationArea');
      const notif = document.createElement('div');
      notif.className = `notification ${type}`;
      notif.textContent = message;
      area.appendChild(notif);
      setTimeout(() => {
        notif.style.animation = 'slideOut 0.3s ease';
        setTimeout(() => notif.remove(), 300);
      }, duration);
    }

    function saveConfig() {
      const form = document.getElementById('configForm');
      const formData = new FormData(form);
      
      showNotification('⏳ 正在保存配置...', 'info', 10000);
      
      fetch('/api/save_config', {
        method: 'POST',
        body: formData
      })
      .then(r => r.json())
      .then(data => {
        if (data.ok) {
          showNotification('✅ 配置已保存并生效！', 'success', 3500);
          setTimeout(() => location.reload(), 500);
        } else {
          showNotification('❌ 保存失败：' + (data.error || '未知错误'), 'error');
        }
      })
      .catch(err => {
        showNotification('❌ 请求失败：' + err, 'error');
      });
    }

    async function previewEmail() {
      showNotification('⏳ 正在生成邮件预览...', 'info', 10000);
      try {
        const res = await fetch('/api/preview_email');
        const data = await res.json();
        if (data.status === 'ok') {
          document.getElementById('previewSubject').textContent = data.subject;
          document.getElementById('previewCount').textContent = data.message_count;
          document.getElementById('previewContent').innerHTML = data.body;
          document.getElementById('previewModal').style.display = 'flex';
          showNotification('✅ 预览已加载', 'success');
        } else {
          showNotification(data.message || '预览失败', 'error');
        }
      } catch (err) {
        showNotification('❌ 请求失败', 'error');
      }
    }

    function closePreview() {
      document.getElementById('previewModal').style.display = 'none';
    }

    async function sendPreviewEmail() {
      if (confirm('⚠️ 确认发送此邮件？')) {
        showNotification('⏳ 正在发送邮件...', 'info', 10000);
        try {
          const res = await fetch('/api/trigger_manual');
          const data = await res.json();
          if (data.status === 'ok') {
            showNotification(data.message, 'success', 4000);
            setTimeout(() => closePreview(), 1000);
          } else {
            showNotification(data.message || '发送失败', 'error');
          }
        } catch (err) {
          showNotification('❌ 请求失败', 'error');
        }
      }
    }

    async function triggerManual() {
      showNotification('⏳ 正在执行邮件汇总...', 'info', 10000);
      try {
        const res = await fetch('/api/trigger_manual');
        const data = await res.json();
        if (data.status === 'ok') {
          showNotification(data.message, 'success', 4000);
        } else {
          showNotification(data.message || '执行失败', 'error');
        }
      } catch (err) {
        showNotification('❌ 请求失败', 'error');
      }
    }

    function exportDatabase() {
      showNotification('⏳ 正在导出数据库...', 'info');
      fetch('/db/messages')
        .then(r => r.text())
        .then(() => {
          const link = document.createElement('a');
          link.href = 'file:///C:/Users/Hikoo/Desktop/信息汇总收集/napcat_messages.db';
          link.download = 'napcat_messages.db';
          link.click();
          showNotification('✅ 数据库文件位置：./napcat_messages.db', 'success');
        })
        .catch(() => showNotification('❌ 导出失败', 'error'));
    }

    async function clearAllMessages() {
      try {
        const res = await fetch('/api/clear_all_messages', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ confirm: true })
        });
        const data = await res.json();
        if (data.ok) {
          showNotification('✅ ' + data.message, 'success');
          setTimeout(() => location.reload(), 500);
        } else {
          showNotification('❌ ' + data.error, 'error');
        }
      } catch (err) {
        showNotification('❌ 清空失败', 'error');
      }
    }
  </script>
</body>
</html>
"""

DB_MESSAGES_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
  <title>数据库管理</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding: 24px; }
    .container { max-width: 1200px; margin: 0 auto; }
    .header { background: white; padding: 24px; border-radius: 16px; margin-bottom: 24px; box-shadow: 0 10px 40px rgba(0,0,0,0.1); display: flex; justify-content: space-between; align-items: center; }
    .header h1 { font-size: 26px; background: linear-gradient(135deg, #FF6B9D, #FF8FB3); -webkit-background-clip: text; -webkit-text-fill-color: transparent; font-weight: 700; }
    .nav { display: flex; gap: 8px; }
    .nav a { padding: 8px 16px; border-radius: 8px; text-decoration: none; font-size: 14px; font-weight: 500; background: rgba(255,107,157,0.1); color: #FF6B9D; transition: all 0.3s; }
    .nav a.active { background: linear-gradient(135deg, #FF6B9D, #FF8FB3); color: white; }
    .nav a:hover { transform: translateY(-2px); }
    .card { background: white; border-radius: 16px; padding: 24px; box-shadow: 0 10px 40px rgba(0,0,0,0.08); margin-bottom: 24px; }
    .toolbar { display: flex; gap: 12px; margin-bottom: 20px; align-items: center; flex-wrap: wrap; }
    .toolbar .btn { padding: 8px 16px; border-radius: 8px; border: none; cursor: pointer; font-size: 13px; font-weight: 600; transition: all 0.3s; }
    .toolbar .btn-sm { background: rgba(255,107,157,0.1); color: #FF6B9D; }
    .toolbar .btn-sm:hover { background: rgba(255,107,157,0.2); transform: translateY(-1px); }
    .toolbar .btn-danger { background: #FF6B6B; color: white; }
    .toolbar .btn-danger:hover { background: #FF5252; }
    table { width: 100%; border-collapse: collapse; font-size: 14px; }
    th { background: linear-gradient(135deg, rgba(255,107,157,0.1), rgba(255,143,179,0.1)); padding: 14px; text-align: left; font-weight: 700; color: #FF6B9D; border-bottom: 2px solid #FF8FB3; }
    td { padding: 12px 14px; border-bottom: 1px solid #E8EEF5; color: #2C3E50; }
    tr:hover { background: #F9FAFC; }
    .checkbox { cursor: pointer; width: 18px; height: 18px; accent-color: #FF8FB3; }
    .btn-detail { padding: 6px 12px; border: none; border-radius: 6px; cursor: pointer; font-size: 13px; font-weight: 600; background: rgba(255,107,157,0.15); color: #FF6B9D; border: 1px solid #FF8FB3; transition: all 0.3s; }
    .btn-detail:hover { background: rgba(255,107,157,0.25); transform: translateY(-2px); }
    .pagination { display: flex; justify-content: space-between; align-items: center; margin-top: 24px; padding-top: 20px; border-top: 2px solid #E8EEF5; }
    .pagination a { padding: 8px 16px; border-radius: 8px; background: rgba(255,107,157,0.1); color: #FF6B9D; text-decoration: none; font-weight: 600; transition: all 0.3s; }
    .pagination a:hover { background: rgba(255,107,157,0.2); transform: translateY(-2px); }
    @media (max-width: 768px) {
      .toolbar { flex-direction: column; gap: 8px; }
      .toolbar input, .toolbar .btn { width: 100%; }
      table { font-size: 12px; }
      th, td { padding: 8px 6px; }
      .btn-detail { padding: 4px 8px; font-size: 12px; }
      .pagination { flex-direction: column; gap: 12px; }
      .pagination a { width: 100%; text-align: center; }
      .header { flex-direction: column; }
    }
    .modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5); z-index: 1000; align-items: center; justify-content: center; }
    .modal.active { display: flex; }
    .modal-content { background: white; border-radius: 16px; padding: 28px; max-width: 600px; max-height: 80vh; overflow-y: auto; box-shadow: 0 20px 60px rgba(0,0,0,0.3); }
    .modal-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; padding-bottom: 16px; border-bottom: 2px solid #FF8FB3; }
    .modal-header h3 { font-size: 18px; color: #FF6B9D; margin: 0; }
    .close-btn { background: rgba(255,107,157,0.1); border: none; border-radius: 6px; padding: 6px 12px; cursor: pointer; color: #FF6B9D; font-weight: 600; transition: all 0.3s; }
    .close-btn:hover { background: rgba(255,107,157,0.2); }
    pre { background: #F5F7FA; padding: 16px; border-radius: 8px; overflow-x: auto; color: #2C3E50; font-size: 12px; }
    .notification { position: fixed; top: 20px; right: 20px; background: white; padding: 16px 24px; border-radius: 12px; box-shadow: 0 10px 40px rgba(0,0,0,0.2); border-left: 4px solid #FF6B9D; animation: slideIn 0.3s ease; z-index: 999; max-width: 400px; }
    .notification.success { border-left-color: #2ECC71; }
    .notification.error { border-left-color: #FF6B6B; }
    @keyframes slideIn { from { transform: translateX(400px); opacity: 0; } to { transform: translateX(0); opacity: 1; } }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div>
        <h1>📊 数据库管理</h1>
        <p style="color: #7F8FA3; margin-top: 4px;">共 {{total}} 条记录 · 第 {{page}} / {{total_pages}} 页</p>
      </div>
      <div class="nav">
        <a href="/">⚙️ 配置</a>
        <a href="/db/messages" class="active">📊 消息</a>
        <a href="/db/ai">🤖 摘要</a>
      </div>
    </div>

    <div id="notificationArea"></div>

    <div class="card">
      <div class="toolbar">
        <input type="text" id="searchInput" placeholder="🔍 搜索内容/发送者/群名..." style="flex: 1; padding: 8px 12px; border: 2px solid #E8EEF5; border-radius: 8px; font-size: 13px;">
        <input type="date" id="dateStart" style="padding: 8px 12px; border: 2px solid #E8EEF5; border-radius: 8px; font-size: 13px;">
        <input type="date" id="dateEnd" style="padding: 8px 12px; border: 2px solid #E8EEF5; border-radius: 8px; font-size: 13px;">
        <button class="btn btn-sm" onclick="applySearch()">🔍 搜索</button>
        <button class="btn btn-sm" onclick="clearSearch()">✕ 清除</button>
      </div>
      <div id="searchResults" style="display: none; margin-bottom: 16px; padding: 12px; background: #F0FFF4; border-left: 4px solid #2ECC71; border-radius: 8px; font-size: 13px;">
        找到 <span id="resultCount">0</span> 条结果
      </div>
        <button class="btn btn-sm" onclick="batchUnmark()">⏳ 标记未处理</button>
        <button class="btn btn-danger" onclick="batchDelete()">🗑️ 删除选中</button>
      </div>

      <table>
        <thead>
          <tr>
            <th style="width: 40px;"><input type="checkbox" id="headerCheckbox" class="checkbox"></th>
            <th style="width: 60px;">ID</th>
            <th>时间</th>
            <th>类型</th>
            <th>群/私聊</th>
            <th>发送者</th>
            <th>内容</th>
            <th style="width: 80px;">已处理</th>
            <th style="width: 80px;">操作</th>
          </tr>
        </thead>
        <tbody>
          {% for m in messages %}
          <tr>
            <td><input type="checkbox" class="checkbox row-checkbox" data-id="{{m['id']}}"></td>
            <td>#{{m['id']}}</td>
            <td style="font-size:12px;">{{m['received_at']}}</td>
            <td><span style="background:rgba(255,107,157,0.1);color:#FF6B9D;padding:4px 8px;border-radius:4px;font-weight:600;">{{m['message_type']}}</span></td>
            <td>{{ (m['group_name'] or m['group_id']) if m['group_id'] else ('💬'+ (m['sender_name'] or m['user_id'] or '')) }}</td>
            <td>{{m['sender_name'] or m['user_id']}}</td>
            <td style="max-width:200px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{{m['content'][:60]}}</td>
            <td><input type="checkbox" class="checkbox proc" data-id="{{m['id']}}" {% if m['processed'] %}checked{% endif %}></td>
            <td><button class="btn-detail" onclick="showDetail({{m['id']}})">详情</button></td>
          </tr>
          {% endfor %}
        </tbody>
      </table>

      <div class="pagination">
        <div>第 {{page}} / {{total_pages}} 页</div>
        <div>
          {% if page>1 %}<a href="/db/messages?page={{page-1}}">← 上一页</a>{% endif %}
          {% if page<total_pages %}<a href="/db/messages?page={{page+1}}">下一页 →</a>{% endif %}
        </div>
      </div>
    </div>
  </div>

  <div class="modal" id="detailModal">
    <div class="modal-content">
      <div class="modal-header">
        <h3>💬 消息详情</h3>
        <button class="close-btn" onclick="closeDetail()">关闭</button>
      </div>
      <pre id="detailContent"></pre>
    </div>
  </div>

  <script>
    let allMessages = [];

    // 页面加载时获取所有消息
    window.addEventListener('load', async () => {
      await loadAllMessages();
    });

    async function loadAllMessages() {
      try {
        // 从表格获取所有消息数据
        allMessages = Array.from(document.querySelectorAll('table tbody tr')).map(row => {
          const cells = row.querySelectorAll('td');
          return {
            id: cells[1].textContent.replace('#', ''),
            time: cells[2].textContent,
            type: cells[3].textContent,
            scene: cells[4].textContent,
            sender: cells[5].textContent,
            content: cells[6].textContent,
            element: row
          };
        });
      } catch (err) {
        console.error('加载消息失败', err);
      }
    }

    function applySearch() {
      const query = document.getElementById('searchInput').value.toLowerCase();
      const dateStart = document.getElementById('dateStart').value;
      const dateEnd = document.getElementById('dateEnd').value;

      let results = allMessages.filter(msg => {
        const matchQuery = !query || 
          msg.content.toLowerCase().includes(query) ||
          msg.sender.toLowerCase().includes(query) ||
          msg.scene.toLowerCase().includes(query);
        
        const msgDate = msg.time.split(' ')[0];
        const matchDate = (!dateStart || msgDate >= dateStart) && 
                         (!dateEnd || msgDate <= dateEnd);
        
        return matchQuery && matchDate;
      });

      const tbody = document.querySelector('table tbody');
      allMessages.forEach(msg => msg.element.style.display = 'none');
      results.forEach(msg => msg.element.style.display = '');

      document.getElementById('searchResults').style.display = results.length > 0 ? 'block' : 'none';
      document.getElementById('resultCount').textContent = results.length;
    }

    function clearSearch() {
      document.getElementById('searchInput').value = '';
      document.getElementById('dateStart').value = '';
      document.getElementById('dateEnd').value = '';
      document.getElementById('searchResults').style.display = 'none';
      allMessages.forEach(msg => msg.element.style.display = '');
    }

    function showNotification(message, type='info') {
      const area = document.getElementById('notificationArea');
      const notif = document.createElement('div');
      notif.className = `notification ${type}`;
      notif.textContent = message;
      area.appendChild(notif);
      setTimeout(() => notif.remove(), 3500);
    }

    document.getElementById('headerCheckbox').addEventListener('change', (e) => {
      document.querySelectorAll('.row-checkbox').forEach(cb => cb.checked = e.target.checked);
    });

    document.querySelectorAll('.proc').forEach(cb => {
      cb.addEventListener('change', async (e) => {
        const id = e.target.dataset.id;
        const processed = e.target.checked ? 1 : 0;
        try {
          await fetch('/api/update_message_status', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id, processed })
          });
        } catch (err) {
          showNotification('❌ 更新失败', 'error');
        }
      });
    });

    function getSelectedIds() {
      return Array.from(document.querySelectorAll('.row-checkbox:checked')).map(cb => cb.dataset.id);
    }

    async function batchMark() {
      const ids = getSelectedIds();
      if (!ids.length) { showNotification('⚠️ 请选择消息', 'error'); return; }
      try {
        await fetch('/api/batch_update_status', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ids, processed: 1 })
        });
        showNotification(`✅ 已标记 ${ids.length} 条消息为已处理`, 'success');
        setTimeout(() => location.reload(), 500);
      } catch (err) {
        showNotification('❌ 操作失败', 'error');
      }
    }

    async function batchUnmark() {
      const ids = getSelectedIds();
      if (!ids.length) { showNotification('⚠️ 请选择消息', 'error'); return; }
      try {
        await fetch('/api/batch_update_status', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ids, processed: 0 })
        });
        showNotification(`✅ 已标记 ${ids.length} 条消息为未处理`, 'success');
        setTimeout(() => location.reload(), 500);
      } catch (err) {
        showNotification('❌ 操作失败', 'error');
      }
    }

    async function batchDelete() {
      const ids = getSelectedIds();
      if (!ids.length) { showNotification('⚠️ 请选择消息', 'error'); return; }
      if (!confirm(`⚠️ 确认删除 ${ids.length} 条消息？此操作无法撤销。`)) return;
      try {
        await fetch('/api/batch_delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ids })
        });
        showNotification(`✅ 已删除 ${ids.length} 条消息`, 'success');
        setTimeout(() => location.reload(), 500);
      } catch (err) {
        showNotification('❌ 删除失败', 'error');
      }
    }

    async function showDetail(id) {
      try {
        const r = await fetch('/api/message_detail/' + id);
        const j = await r.json();
        document.getElementById('detailContent').textContent = JSON.stringify(j, null, 2);
        document.getElementById('detailModal').classList.add('active');
      } catch (e) {
        showNotification('❌ 加载失败', 'error');
      }
    }

    function closeDetail() {
      document.getElementById('detailModal').classList.remove('active');
    }
  </script>
</body>
</html>
"""

DB_AI_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
  <title>AI 摘要记录</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding: 24px; }
    .container { max-width: 900px; margin: 0 auto; }
    .header { background: white; padding: 24px; border-radius: 16px; margin-bottom: 24px; box-shadow: 0 10px 40px rgba(0,0,0,0.1); display: flex; justify-content: space-between; align-items: center; }
    .header h1 { font-size: 26px; background: linear-gradient(135deg, #FF6B9D, #FF8FB3); -webkit-background-clip: text; -webkit-text-fill-color: transparent; font-weight: 700; }
    .nav { display: flex; gap: 8px; }
    .nav a { padding: 8px 16px; border-radius: 8px; text-decoration: none; font-size: 14px; font-weight: 500; background: rgba(255,107,157,0.1); color: #FF6B9D; transition: all 0.3s; }
    .nav a.active { background: linear-gradient(135deg, #FF6B9D, #FF8FB3); color: white; }
    .nav a:hover { transform: translateY(-2px); }
    .card { background: white; border-radius: 16px; padding: 24px; box-shadow: 0 10px 40px rgba(0,0,0,0.08); margin-bottom: 24px; }
    .ai-item { background: #F9FAFC; border-left: 4px solid #FF8FB3; padding: 16px; margin-bottom: 16px; border-radius: 8px; transition: all 0.3s; }
    .ai-item:hover { background: #F5F7FA; box-shadow: 0 4px 12px rgba(255,107,157,0.1); }
    .ai-item-meta { color: #7F8FA3; font-size: 13px; margin-bottom: 12px; font-weight: 600; }
    .ai-item-meta span { color: #FF6B9D; font-weight: 700; }
    details { cursor: pointer; }
    summary { color: #FF6B9D; font-weight: 600; padding: 8px; user-select: none; }
    summary:hover { color: #FF8FB3; }
    pre { background: white; border: 1px solid #E8EEF5; padding: 12px; border-radius: 6px; margin-top: 12px; overflow-x: auto; font-size: 12px; color: #2C3E50; }
    .pagination { display: flex; justify-content: space-between; align-items: center; margin-top: 24px; padding-top: 20px; border-top: 2px solid #E8EEF5; }
    .pagination a { padding: 8px 16px; border-radius: 8px; background: rgba(255,107,157,0.1); color: #FF6B9D; text-decoration: none; font-weight: 600; transition: all 0.3s; }
    .pagination a:hover { background: rgba(255,107,157,0.2); transform: translateY(-2px); }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div>
        <h1>🤖 AI 摘要记录</h1>
        <p style="color: #7F8FA3; margin-top: 4px;">共 {{total}} 条 · 第 {{page}} / {{total_pages}} 页</p>
      </div>
      <div class="nav">
        <a href="/">⚙️ 配置</a>
        <a href="/db/messages">📊 消息</a>
        <a href="/db/ai" class="active">🤖 摘要</a>
      </div>
    </div>

    <div class="card">
      {% for a in ais %}
      <div class="ai-item">
        <div class="ai-item-meta">
          [<span>#{{a['id']}}</span>] {{a['ai_created_at']}} | 消息: <span>{{a['messages_ids']}}</span> | 邮件: <span>{{ '✅ 已发送' if a['email_sent'] else '⏳ 未发送' }}</span>
        </div>
        <details>
          <summary>📋 查看 AI 摘要内容</summary>
          <pre>{{a['ai_response']}}</pre>
        </details>
      </div>
      {% endfor %}

      <div class="pagination">
        <div>第 {{page}} / {{total_pages}} 页</div>
        <div>
          {% if page>1 %}<a href="/db/ai?page={{page-1}}">← 上一页</a>{% endif %}
          {% if page<total_pages %}<a href="/db/ai?page={{page+1}}">下一页 →</a>{% endif %}
        </div>
      </div>
    </div>
  </div>
</body>
</html>
"""

# ========== Flask 路由 ==========
@app.route("/", methods=["GET"])
def index():
    cfg = CONFIG.copy()
    if not isinstance(cfg.get("RUN_TIMES"), list):
        cfg["RUN_TIMES"] = [s.strip() for s in str(cfg.get("RUN_TIMES","")).split(",") if s.strip()]
    return render_template_string(INDEX_HTML, cfg=cfg)

@app.route("/api/save_config", methods=["POST"])
def api_save_config():
    """使用 AJAX 保存配置，返回 JSON"""
    try:
        form = request.form
        keys = ["WS_URL","API_BASE_URL","DB_FILE","AI_API_URL","AI_API_KEY","AI_MODEL","SMTP_HOST","SMTP_PORT","SMTP_USER","SMTP_PASSWORD","EMAIL_FROM","EMAIL_TO","EMAIL_SENDER_NAME","BATCH_MAX_MESSAGES","FLASK_HOST","FLASK_PORT","PAGE_SIZE"]
        
        for k in keys:
            if k in form:
                val = form.get(k)
                if k in ("SMTP_PORT","BATCH_MAX_MESSAGES","FLASK_PORT","PAGE_SIZE"):
                    try: 
                        val = int(val)
                    except: 
                        pass
                CONFIG[k] = val
        
        rt = form.get("RUN_TIMES","")
        CONFIG["RUN_TIMES"] = [s.strip() for s in rt.split(",") if s.strip()]
        
        if save_config(CONFIG):
            schedule_jobs()
            print("✓ 配置已更新并重新调度任务")
            return jsonify({"ok": True, "message": "配置已保存"})
        else:
            return jsonify({"ok": False, "error": "保存失败"})
    except Exception as e:
        print(f"保存配置错误：{e}")
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/trigger_manual", methods=["GET"])
def api_trigger_manual():
    """手动触发任务，返回 JSON"""
    res = process_and_send_batch(triggered_by="manual_ui")
    return jsonify(res)

@app.route("/api/last_notification", methods=["GET"])
def api_last_notification():
    """获取最后一条系统通知（用于轮询）"""
    return jsonify(last_notification)

@app.route("/db/messages", methods=["GET"])
def db_messages():
    page = request.args.get("page", 1, type=int)
    page_size = CONFIG.get("PAGE_SIZE", 20)
    with db_lock:
        cur = db_conn.cursor()
        cur.execute("SELECT COUNT(*) FROM messages")
        total = cur.fetchone()[0]
        total_pages = max(1, (total + page_size - 1) // page_size)
        offset = (page-1)*page_size
        cur.execute("SELECT id, msg_id, post_type, message_type, user_id, group_id, group_name, sender_name, content, processed, received_at FROM messages ORDER BY received_at DESC LIMIT ? OFFSET ?", (page_size, offset))
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        messages = [dict(zip(cols, r)) for r in rows]
    return render_template_string(DB_MESSAGES_HTML, messages=messages, page=page, total=total, total_pages=total_pages)

@app.route("/api/update_message_status", methods=["POST"])
def api_update_message_status():
    data = request.json or {}
    mid = data.get("id")
    processed = int(data.get("processed", 0))
    if not mid:
        return jsonify({"ok": False})
    with db_lock:
        cur = db_conn.cursor()
        cur.execute("UPDATE messages SET processed=? WHERE id=?", (processed, mid))
        db_conn.commit()
    return jsonify({"ok": True})

@app.route("/api/batch_update_status", methods=["POST"])
def api_batch_update_status():
    data = request.json or {}
    ids = data.get("ids", [])
    processed = int(data.get("processed", 0))
    if not ids:
        return jsonify({"ok": False, "error": "no ids"})
    with db_lock:
        cur = db_conn.cursor()
        placeholders = ",".join("?" * len(ids))
        cur.execute(f"UPDATE messages SET processed=? WHERE id IN ({placeholders})", (processed, *ids))
        db_conn.commit()
    return jsonify({"ok": True, "count": len(ids)})

@app.route("/api/batch_delete", methods=["POST"])
def api_batch_delete():
    data = request.json or {}
    ids = data.get("ids", [])
    if not ids:
        return jsonify({"ok": False, "error": "no ids"})
    with db_lock:
        cur = db_conn.cursor()
        placeholders = ",".join("?" * len(ids))
        cur.execute(f"DELETE FROM messages WHERE id IN ({placeholders})", ids)
        db_conn.commit()
    return jsonify({"ok": True, "count": len(ids)})

@app.route("/api/message_detail/<int:mid>", methods=["GET"])
def api_message_detail(mid):
    with db_lock:
        cur = db_conn.cursor()
        cur.execute("SELECT * FROM messages WHERE id=?", (mid,))
        row = cur.fetchone()
        if not row:
            return jsonify({})
        cols = [d[0] for d in cur.description]
        return jsonify(dict(zip(cols, row)))

@app.route("/db/ai", methods=["GET"])
def db_ai():
    page = request.args.get("page", 1, type=int)
    page_size = CONFIG.get("PAGE_SIZE", 20)
    with db_lock:
        cur = db_conn.cursor()
        cur.execute("SELECT COUNT(*) FROM ai_responses")
        total = cur.fetchone()[0]
        total_pages = max(1, (total + page_size - 1) // page_size)
        offset = (page-1)*page_size
        cur.execute("SELECT id, messages_ids, ai_response, ai_created_at, email_sent FROM ai_responses ORDER BY ai_created_at DESC LIMIT ? OFFSET ?", (page_size, offset))
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        ais = [dict(zip(cols, r)) for r in rows]
    return render_template_string(DB_AI_HTML, ais=ais, page=page, total=total, total_pages=total_pages)

@app.route("/shutdown", methods=["GET", "POST"])
def shutdown_route():
    threading.Thread(target=graceful_shutdown, daemon=True).start()
    return "正在关闭..."

def graceful_shutdown():
    print("开始优雅停机...")
    stop_event.set()
    stop_ws_client()
    time.sleep(1)
    print("程序已退出")
    os._exit(0)

def _signal_handler(signum, frame):
    print(f"收到信号 {signum}，准备退出...")
    graceful_shutdown()

signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)

def start_flask_thread():
    def run():
        try:
            app.run(host=CONFIG.get("FLASK_HOST", "0.0.0.0"), port=int(CONFIG.get("FLASK_PORT", 8080)), debug=False, use_reloader=False)
        except Exception as e:
            print(f"Flask 启动失败：{e}")
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t

@app.route("/api/system_status", methods=["GET"])
def api_system_status():
    """获取系统状态信息"""
    with db_lock:
        cur = db_conn.cursor()
        cur.execute("SELECT COUNT(*) FROM messages WHERE processed = 0")
        unprocessed = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM messages")
        total_messages = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM ai_responses WHERE email_sent = 1")
        sent_emails = cur.fetchone()[0]
    
    return jsonify({
        "ws_connected": ws_app is not None,
        "unprocessed_messages": unprocessed,
        "total_messages": total_messages,
        "sent_emails": sent_emails,
        "next_run_times": CONFIG.get("RUN_TIMES", [])
    })

@app.route("/api/get_groups", methods=["GET"])
def api_get_groups():
    """获取所有群列表和过滤配置"""
    with db_lock:
        cur = db_conn.cursor()
        # 获取所有群组
        cur.execute("SELECT DISTINCT group_id, group_name FROM messages WHERE group_id IS NOT NULL AND group_id != '' ORDER BY group_id")
        rows = cur.fetchall()
        groups = [{"id": r[0], "name": r[1] or f"未知群({r[0]})"} for r in rows]
    
    blacklist = [str(g) for g in CONFIG.get("GROUP_BLACKLIST", [])]
    whitelist = [str(g) for g in CONFIG.get("GROUP_WHITELIST", [])]
    mode = CONFIG.get("GROUP_FILTER_MODE", "blacklist")
    
    return jsonify({
        "groups": groups,
        "filter_mode": mode,
        "blacklist": blacklist,
        "whitelist": whitelist,
        "private_enabled": CONFIG.get("PRIVATE_CHAT_ENABLED", True)
    })

@app.route("/api/update_group_filter", methods=["POST"])
def api_update_group_filter():
    """更新群组过滤配置"""
    data = request.json or {}
    mode = data.get("mode", "blacklist")
    blacklist = data.get("blacklist", [])
    whitelist = data.get("whitelist", [])
    private_enabled = data.get("private_enabled", True)
    
    CONFIG["GROUP_FILTER_MODE"] = mode
    CONFIG["GROUP_BLACKLIST"] = blacklist
    CONFIG["GROUP_WHITELIST"] = whitelist
    CONFIG["PRIVATE_CHAT_ENABLED"] = private_enabled
    
    if save_config(CONFIG):
        return jsonify({"ok": True, "message": "群组过滤配置已更新"})
    else:
        return jsonify({"ok": False, "error": "保存失败"})

@app.route("/api/preview_email", methods=["GET"])
def api_preview_email():
    """生成邮件预览（不发送）"""
    msgs = fetch_unprocessed_messages(limit=CONFIG.get("BATCH_MAX_MESSAGES"))
    if not msgs:
        return jsonify({"status": "no_messages", "message": "没有未处理消息"})
    
    payload = build_ai_payload(msgs)
    ai_raw = call_ai_api(payload)
    if not ai_raw:
        return jsonify({"status": "ai_failed", "message": "AI 接口失败"})
    
    ai_data = extract_ai_json(ai_raw)
    
    try:
        times = [m.get("received_at") for m in msgs if m.get("received_at")]
        if times:
            tmin = min(times)
            tmax = max(times)
            subject = f"📬 QQ 摘要 - {tmin[:16]} ~ {tmax[:16]}"
        else:
            subject = f"📬 QQ 摘要 - {datetime.now().strftime('%Y-%m-%d')}"
    except:
        subject = f"📬 QQ 摘要 - {datetime.now().strftime('%Y-%m-%d')}"
    
    body_html = generate_email_html(ai_data, msgs)
    
    return jsonify({
        "status": "ok",
        "subject": subject,
        "body": body_html,
        "message_count": len(msgs)
    })
def api_clear_all_messages():
    """清空所有消息（谨慎使用）"""
    if not request.json.get("confirm"):
        return jsonify({"ok": False, "error": "需要确认"})
    try:
        with db_lock:
            cur = db_conn.cursor()
            cur.execute("DELETE FROM messages")
            db_conn.commit()
        return jsonify({"ok": True, "message": "所有消息已清空"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

if __name__ == "__main__":
    print("="*60)
    print("启动 NapCat AI 邮件助手")
    print("="*60)
    start_flask_thread()
    schedule_jobs()
    start_ws_client_in_thread(stop_event)
    try:
        while not stop_event.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:

        graceful_shutdown()
