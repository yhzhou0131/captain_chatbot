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
Status = {
    'normal': 0,
    'check_reset_time': 1,
    'no_reminder_time': 2,
    'reset_time': 3,
    'query_reminder': 4
}

UserInput = {
    Status['reset_time']: '修改提醒時間',
    Status['query_reminder']: '查詢提醒時間'
}

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

app.config['SQLALCHEMY_DATABASE_URI'] = f'mysql+pymysql://{quote_plus(MYSQL_USER)}:{quote_plus(MYSQL_PASSWORD or "")}@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DB}'
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
    conv_status = db.Column(db.Integer, default=Status['normal'])  # 對話狀態存 DB，serverless 不保留記憶體
    conv_med = db.Column(db.String(10), nullable=True)

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
    register_user(event.source.user_id)

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_input = event.message.text.strip()
    register_user(event.source.user_id)
    state = get_state()
    status = state.conv_status

    if status == Status['normal']:
        if user_input == UserInput[Status['query_reminder']]:
            query_reminder_date(event)
        elif user_input == UserInput[Status['reset_time']]:
            buttons_template = TemplateSendMessage(
                alt_text='提醒訊息',
                template=ButtonsTemplate(
                    text="想要修改或設定的是犬新寶還是一錠除的時間呢？",
                    actions=[
                        PostbackAction(label="犬新寶", data='{"action":"update_reminder", "type":"heartgard"}'),
                        PostbackAction(label="一錠除", data='{"action":"update_reminder", "type":"bravecto"}')
                    ]
                )
            )
            line_bot_api.push_message(event.source.user_id, buttons_template)
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="目前的有效指令為：\n1. 修改提醒時間\n2. 查詢提醒時間"))
    elif status == Status['check_reset_time']:
        match = re.match(r'^\d{4}/\d{2}/\d{2}$', user_input)
        if match:
            # 使用本地時區創建日期（設定為當天 00:00:00 台灣時間）
            new_date = datetime.strptime(user_input, "%Y/%m/%d").replace(tzinfo=TZ_TW)
            if new_date.date() < datetime.now(TZ_TW).date():
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="此為過去時間，請重新輸入提醒時間。"))
                return
            update_reminder_type = state.conv_med
            if update_reminder_type is None:
                app.logger.error("update_reminder_type 為空！")
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="後端有問題，請聯繫昱豪！"))
                return
            setattr(state, f"{update_reminder_type}_date", new_date)
            setattr(state, f"{update_reminder_type}_status", 'pending')
            state.conv_status = Status['normal']
            db.session.commit()
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"完成設定，下次餵 {Medicine[update_reminder_type]} 的日期為：{new_date.strftime('%Y/%m/%d')}。"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="時間輸入格式錯誤，請重新輸入提醒時間。"))
    else:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"狀態錯誤，請通知昱豪！\nStatus = {status}"))

# --------------------------- 功能：查詢提醒 ---------------------------
def query_reminder_date(event):
    state = get_state()
    if not state.bravecto_date and not state.heartgard_date:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="目前沒有提醒時間，請輸入「修改提醒時間」進行設定。"))
    elif not state.bravecto_date:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=f"下次餵 {Medicine['heartgard']} 的日期為：{state.heartgard_date.strftime('%Y/%m/%d')}\n目前沒有設定 {Medicine['bravecto']} 的提醒時間，請輸入「修改提醒時間」"))
    elif not state.heartgard_date:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=f"下次餵 {Medicine['bravecto']} 的日期為：{state.bravecto_date.strftime('%Y/%m/%d')}\n目前沒有設定 {Medicine['heartgard']} 的提醒時間，請輸入「修改提醒時間」"))
    else:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=f"下次餵{Medicine['bravecto']}的日期為：{state.bravecto_date.strftime('%Y/%m/%d')}\n下次餵{Medicine['heartgard']}的日期為：{state.heartgard_date.strftime('%Y/%m/%d')}"))

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

        buttons_template = TemplateSendMessage(
            alt_text='提醒訊息',
            template=ButtonsTemplate(
                text=f"今天是餵隊長{Medicine[med]}的日子！請確認是否完成！",
                actions=[
                    PostbackAction(label="完成餵藥", data=f'{{"action":"done_medicine","type":"{med}"}}'),
                    PostbackAction(label="明天再提醒", data=f'{{"action":"delay_medicine","type":"{med}"}}')
                ]
            )
        )
        # 推播給所有註冊使用者
        for usr in LineUser.query.all():
            try:
                line_bot_api.push_message(usr.user_id, buttons_template)
            except Exception as e:
                app.logger.warning(f"推送給 {usr.user_id} 失敗：{e}")

            # 推播完標記，避免同一天重複
            setattr(state, f"{med}_status", "waiting")
        db.session.commit()   # 記得提交

# --------------------------- Postback 處理 ---------------------------
@handler.add(PostbackEvent)
def handle_postback(event):
    register_user(event.source.user_id)
    state = get_state()

    data = json.loads(event.postback.data)
    action = data.get("action")
    med_type = data.get("type")
    today = datetime.now(TZ_TW).date()
    current_date = getattr(state, f"{med_type}_date")
    
    # 確保 current_date 有時區資訊
    if current_date and current_date.tzinfo is None:
        current_date = current_date.replace(tzinfo=TZ_TW)

    if action == "update_reminder":
        state.conv_med = med_type
        state.conv_status = Status['check_reset_time']
        db.session.commit()
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="請輸入想要提醒的時間（YYYY/MM/DD）。"))

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
        state.conv_status = Status['normal']
        db.session.commit()

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"好的，{Medicine[med_type]}的下次提醒時間為 {next_date.strftime('%Y/%m/%d')}"))

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

# --------------------------- 入口 ---------------------------
if __name__ == "__main__":
    with app.app_context():
        db.create_all()          # 確保資料表存在
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
