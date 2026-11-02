from flask import Flask, request, abort
from flask_sqlalchemy import SQLAlchemy
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import *
import os, re, json, hmac
from urllib.parse import quote_plus
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import logging
from sqlalchemy.exc import OperationalError

# 定義台灣時區 (UTC+8)
TZ_TW = timezone(timedelta(hours=8))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# --------------------------- 保持原文字常數 ---------------------------
CARD_KEYWORDS = {"隊長", "查詢提醒時間", "修改提醒時間"}  # 群組中只有這些關鍵字才會叫出狀態卡

Medicine = {
    'bravecto': '一錠除',
    'heartgard': '犬新寶'
}

# --------------------------- Flask & MySQL 初始化 ---------------------------
app = Flask(__name__)
load_dotenv()

MYSQL_USER = os.getenv("MYSQL_USER", "EasonAndClaire")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD")
MYSQL_DB = os.getenv("MYSQL_DB", "EasonAndClaire$default")
MYSQL_HOST = os.getenv("MYSQL_HOST", "EasonAndClaire.mysql.pythonanywhere-services.com")
MYSQL_PORT = os.getenv("MYSQL_PORT", "3306")

app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv("DATABASE_URL") or f'mysql+pymysql://{quote_plus(MYSQL_USER)}:{quote_plus(MYSQL_PASSWORD or "")}@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DB}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
# TiDB 公網連線閒置 340 秒會被切斷，連線池需在此之前回收
engine_options = {"pool_recycle": 280, "pool_pre_ping": True}
if os.getenv("MYSQL_SSL"):  # TiDB Starter 強制 TLS；PythonAnywhere 不需要
    import certifi
    engine_options["connect_args"] = {"ssl": {"ca": certifi.where()}}
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = engine_options

db = SQLAlchemy(app)

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
CRON_SECRET = os.getenv("CRON_SECRET")
if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    raise ValueError("LINE bot token/secret 未設定，請檢查 .env")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --------------------------- 資料表定義 ---------------------------
class ReminderState(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    bravecto_date = db.Column(db.DateTime, nullable=True)
    heartgard_date = db.Column(db.DateTime, nullable=True)
    bravecto_status = db.Column(db.String(10), default='pending')  # 'pending' / 'waiting'
    heartgard_status = db.Column(db.String(10), default='pending')

class LineUser(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(64), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

# --------------------------- 輔助函式 ---------------------------

def get_state():
    try:
        state = ReminderState.query.get(1)
    except OperationalError as e:
        app.logger.warning(f"資料庫連線失敗，重新嘗試：{e}")
        db.session.rollback()
        db.session.close()
        state = ReminderState.query.get(1)

    if not state:
        state = ReminderState(id=1)
        db.session.add(state)
        db.session.commit()
    return state

def register_user(user_id: str):
    if not LineUser.query.filter_by(user_id=user_id).first():
        db.session.add(LineUser(user_id=user_id))
        db.session.commit()

# 推播目標：群組/多人聊天登記聊天室 ID（群組成員未必是好友，推播到群組即可涵蓋）；一對一登記使用者 ID
# LineUser.user_id 欄位同時存放 user/group/room ID（ID 前綴不同不會衝突），避免資料表遷移
def chat_id(source) -> str:
    return getattr(source, "group_id", None) or getattr(source, "room_id", None) or source.user_id

def unregister(target_id: str):
    LineUser.query.filter_by(user_id=target_id).delete()
    db.session.commit()

def display_name(source) -> str:
    try:
        if source.type == "group":
            return line_bot_api.get_group_member_profile(source.group_id, source.user_id).display_name
        if source.type == "room":
            return line_bot_api.get_room_member_profile(source.room_id, source.user_id).display_name
        return line_bot_api.get_profile(source.user_id).display_name
    except Exception as e:
        app.logger.warning(f"取得顯示名稱失敗：{e}")
        return "有人"

def broadcast(text: str, exclude: str):
    # exclude：事件發生的聊天室，已用 reply 回覆過，不重複推播
    for usr in LineUser.query.filter(LineUser.user_id != exclude).all():
        try:
            line_bot_api.push_message(usr.user_id, TextSendMessage(text=text))
        except Exception as e:
            app.logger.warning(f"推送給 {usr.user_id} 失敗：{e}")

# --------------------------- 基本路由 ---------------------------
@app.route('/')
def home():
    return "用藥提醒機器人運行中！"

@app.route('/callback', methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@app.route('/trigger_reminder', methods=['GET'])
def trigger_reminder():
    # Vercel cron 會自動帶 "Authorization: Bearer $CRON_SECRET"；未設定時一律拒絕
    if not CRON_SECRET or not hmac.compare_digest(request.headers.get("Authorization", ""), f"Bearer {CRON_SECRET}"):
        abort(401)
    check_reminder()
    return "Reminder triggered!", 200

# --------------------------- 事件處理 ---------------------------
@handler.add(JoinEvent)
def handle_join(event):
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="感謝邀請我加入！"))
    register_user(chat_id(event.source))

@handler.add(FollowEvent)
def handle_follow(event):
    register_user(chat_id(event.source))

@handler.add(UnfollowEvent)
def handle_unfollow(event):
    unregister(chat_id(event.source))

@handler.add(LeaveEvent)
def handle_leave(event):
    unregister(chat_id(event.source))

def med_buttons(med):
    return [QuickReplyButton(action=PostbackAction(label=f"完成餵{Medicine[med]}", data=json.dumps({"action": "done_medicine", "type": med}))),
            QuickReplyButton(action=PostbackAction(label=f"{Medicine[med]}明天再提醒", data=json.dumps({"action": "delay_medicine", "type": med})))]

# Quick Reply 按鈕在聊天室出現任何新訊息（含 bot 的回覆）後會消失，達到「有人按了就收合」；已發出的訊息無法收回
def status_card(state):
    today = datetime.now(TZ_TW).date()
    items, lines = [], []
    for med in ("heartgard", "bravecto"):
        d = getattr(state, f"{med}_date")
        lines.append(f"{Medicine[med]}：{d.strftime('%Y/%m/%d') if d else '尚未設定'}")
        items.append(QuickReplyButton(action=DatetimePickerAction(
            label=f"設定{Medicine[med]}日期", data=json.dumps({"action": "set_date", "type": med}),
            mode="date", initial=(max(d.date(), today) if d else today).isoformat(), min=today.isoformat())))
        if getattr(state, f"{med}_status") == 'waiting':
            items += med_buttons(med)
    return TextSendMessage(text="隊長的餵藥日期\n" + "\n".join(lines), quick_reply=QuickReply(items=items))

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    register_user(chat_id(event.source))
    # 一對一任何文字都叫出狀態卡；群組只回應關鍵字，避免洗版
    if event.source.type == "user" or event.message.text.strip() in CARD_KEYWORDS:
        line_bot_api.reply_message(event.reply_token, status_card(get_state()))

# --------------------------- 功能：每日檢查並推播 ---------------------------
def check_reminder():
    # 使用台灣時區來取得今天的日期
    today = datetime.now(TZ_TW).date()
    state = get_state()

    for med in ['bravecto', 'heartgard']:
        med_date = getattr(state, f"{med}_date")
        med_status = getattr(state, f"{med}_status")

        if not med_date:
            continue
        
        # 確保 med_date 有時區資訊，如果沒有則假設為台灣時區
        if med_date.tzinfo is None:
            med_date = med_date.replace(tzinfo=TZ_TW)
        
        # 比較日期（使用台灣時區）
        if med_date.date() != today or med_status != 'pending':
            continue  # 只針對今日且 pending 的提醒

        text = f"今天是餵隊長{Medicine[med]}的日子！請確認是否完成！"
        quick_msg = TextSendMessage(text=text, quick_reply=QuickReply(items=med_buttons(med)))
        # 群組/多人聊天常被閒聊洗掉 Quick Reply（任何新訊息都會讓按鈕消失），改用不會消失的按鈕卡；ID 前綴 C=群組、R=多人聊天、U=使用者
        card_msg = TemplateSendMessage(alt_text='提醒訊息', template=ButtonsTemplate(
            text=text, actions=[b.action for b in med_buttons(med)]))
        # 推播給所有註冊使用者
        for usr in LineUser.query.all():
            try:
                line_bot_api.push_message(usr.user_id, card_msg if usr.user_id[0] in "CR" else quick_msg)
            except Exception as e:
                app.logger.warning(f"推送給 {usr.user_id} 失敗：{e}")

            # 推播完標記，避免同一天重複
            setattr(state, f"{med}_status", "waiting")
        db.session.commit()   # 記得提交

# --------------------------- Postback 處理 ---------------------------
@handler.add(PostbackEvent)
def handle_postback(event):
    register_user(chat_id(event.source))
    state = get_state()

    data = json.loads(event.postback.data)
    action = data.get("action")
    if action == "show":
        line_bot_api.reply_message(event.reply_token, status_card(state))
        return
    med_type = data.get("type")
    today = datetime.now(TZ_TW).date()
    current_date = getattr(state, f"{med_type}_date")
    
    # 確保 current_date 有時區資訊
    if current_date and current_date.tzinfo is None:
        current_date = current_date.replace(tzinfo=TZ_TW)

    if action == "set_date":
        new_date = datetime.strptime(event.postback.params["date"], "%Y-%m-%d").replace(tzinfo=TZ_TW)
        if new_date.date() < today:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="此為過去時間，請重新選擇。"))
            return
        setattr(state, f"{med_type}_date", new_date)
        setattr(state, f"{med_type}_status", 'pending')
        db.session.commit()
        date_text = new_date.strftime('%Y/%m/%d')
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"完成設定，下次餵 {Medicine[med_type]} 的日期為：{date_text}。"))
        broadcast(f"{display_name(event.source)} 設定下次餵 {Medicine[med_type]} 的日期為 {date_text}", chat_id(event.source))

    elif action == "done_medicine":
        # 只允許 waiting
        if getattr(state, f"{med_type}_status") != 'waiting':
            line_bot_api.reply_message(event.reply_token,
                TextSendMessage(text=f"已經完成過 {Medicine[med_type]} 的餵藥，不需要再操作。"))
            return

        # 若日期已經往後 (> 今天) 說明別人操作過
        if current_date and current_date.date() > today:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"已經完成過 {Medicine[med_type]} 的餵藥，不需要再操作。"))
            return

        # 順延日期
        delta = 90 if med_type == 'bravecto' else 30
        base_date = current_date if current_date else datetime.now(TZ_TW)
        next_date = base_date + timedelta(days=delta)
        setattr(state, f"{med_type}_date", next_date)
        setattr(state, f"{med_type}_status", 'pending')   # 重設為 pending
        db.session.commit()

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"好的，{Medicine[med_type]}的下次提醒時間為 {next_date.strftime('%Y/%m/%d')}"))
        broadcast(f"{display_name(event.source)} 已完成餵 {Medicine[med_type]}，下次提醒時間為 {next_date.strftime('%Y/%m/%d')}", chat_id(event.source))

    elif action == "delay_medicine":
        if getattr(state, f"{med_type}_status") != 'waiting':
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"已經處理過 {Medicine[med_type]} 的提醒了。"))
            return

        # 若日期已經往後 (> 今天) 說明別人操作過
        if current_date and current_date.date() > today:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"已經完成過 {Medicine[med_type]} 的餵藥，不需要再操作。"))
            return

        tomorrow = datetime.now(TZ_TW) + timedelta(days=1)
        setattr(state, f"{med_type}_date", tomorrow)
        setattr(state, f"{med_type}_status", 'pending')
        db.session.commit()
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"{Medicine[med_type]}的下次提醒時間設為隔日 {tomorrow.strftime('%Y/%m/%d')} 送出提醒"))
        broadcast(f"{display_name(event.source)} 選擇明天再提醒餵 {Medicine[med_type]}，將於 {tomorrow.strftime('%Y/%m/%d')} 再次提醒", chat_id(event.source))

# --------------------------- 入口 ---------------------------
if __name__ == "__main__":
    with app.app_context():
        db.create_all()          # 確保資料表存在
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
