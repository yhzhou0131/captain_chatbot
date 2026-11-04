"""python -m unittest discover -s tests  （需先 pip install -r requirements.txt）"""
import json
import os
import struct
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock
from types import SimpleNamespace as NS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "scripts")]
# 一律用記憶體 SQLite，絕不連到正式資料庫
os.environ.update(DATABASE_URL="sqlite://", LINE_CHANNEL_ACCESS_TOKEN="x", LINE_CHANNEL_SECRET="x",
                  MYSQL_SSL="", CRON_SECRET="s3cret")

import captain_chat_bot as bot  # noqa: E402
import setup_rich_menu  # noqa: E402

TZ = bot.TZ_TW
USER = NS(type="user", user_id="U1")
GROUP = NS(type="group", group_id="Cg1", user_id="U2")


class FakeApi:
    def __init__(self):
        self.replies, self.pushes = [], []

    def reply_message(self, token, msg):
        self.replies.append((token, msg))

    def push_message(self, to, msg):
        self.pushes.append((to, msg))

    def get_profile(self, uid):
        return NS(display_name="阿姨")

    def get_group_member_profile(self, gid, uid):
        return NS(display_name="小明")


def msg(source, text, token="t"):
    return NS(source=source, message=NS(text=text), reply_token=token)


def pb(source, data, params=None, token="t"):
    return NS(source=source, reply_token=token, postback=NS(data=json.dumps(data), params=params))


def jpeg_size(data):
    i = 2
    while i < len(data):
        marker, length = data[i + 1], struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2):  # SOF：高、寬
            height, width = struct.unpack(">HH", data[i + 5:i + 9])
            return width, height
        i += 2 + length


def day(offset):
    return (datetime.now(TZ) + timedelta(days=offset)).strftime("%Y-%m-%d")


class BotTestCase(unittest.TestCase):
    def setUp(self):
        self.assertTrue(bot.app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"))
        self.ctx = bot.app.app_context()
        self.ctx.push()
        bot.db.create_all()
        self.api = bot.line_bot_api = FakeApi()

    def tearDown(self):
        bot.db.session.remove()
        bot.db.drop_all()
        self.ctx.pop()

    def ids(self):
        return sorted(u.user_id for u in bot.LineUser.query.all())

    def set_med(self, med, offset=0, status="pending"):
        st = bot.get_state()
        setattr(st, f"{med}_date", datetime.now(TZ) + timedelta(days=offset))
        setattr(st, f"{med}_status", status)
        bot.db.session.commit()


class CronAuth(BotTestCase):
    def test_requires_matching_bearer_secret(self):
        c = bot.app.test_client()
        with mock.patch.object(bot, "check_reminder") as check:
            self.assertEqual(c.get("/trigger_reminder").status_code, 401)
            self.assertEqual(c.get("/trigger_reminder", headers={"Authorization": "Bearer wrong"}).status_code, 401)
            check.assert_not_called()
            self.assertEqual(c.get("/trigger_reminder", headers={"Authorization": "Bearer s3cret"}).status_code, 200)
            check.assert_called_once()

    def test_rejects_when_secret_unset(self):
        with mock.patch.object(bot, "check_reminder") as check, mock.patch.object(bot, "CRON_SECRET", None):
            r = bot.app.test_client().get("/trigger_reminder", headers={"Authorization": "Bearer None"})
            self.assertEqual(r.status_code, 401)
            check.assert_not_called()


class Registration(BotTestCase):
    def test_group_message_registers_group_not_member(self):
        bot.handle_message(msg(GROUP, "閒聊"))
        bot.handle_message(msg(USER, "hi"))
        self.assertEqual(self.ids(), ["Cg1", "U1"])

    def test_follow_unfollow_leave_join(self):
        bot.handle_follow(NS(source=USER))
        self.assertEqual(self.ids(), ["U1"])
        bot.handle_unfollow(NS(source=USER))
        self.assertEqual(self.ids(), [])
        bot.handle_join(NS(source=NS(type="group", group_id="Cg2"), reply_token="t"))  # join 事件沒有 user_id
        self.assertEqual(self.ids(), ["Cg2"])
        welcome = self.api.replies[-1][1]  # 歡迎訊息帶按鈕，點了送出 show
        self.assertEqual(welcome.type, "template")
        self.assertEqual(json.loads(welcome.template.actions[0].data), {"action": "show"})
        json.dumps(welcome.as_json_dict(), ensure_ascii=False)
        bot.handle_leave(NS(source=NS(type="group", group_id="Cg2")))
        self.assertEqual(self.ids(), [])


class StatusCard(BotTestCase):
    def test_one_to_one_any_text_shows_card_with_pickers(self):
        bot.handle_message(msg(USER, "隨便"))
        card = self.api.replies[-1][1]
        actions = [i.action for i in card.quick_reply.items]
        self.assertEqual([a.type for a in actions], ["datetimepicker"] * 2)
        self.assertIn("尚未設定", card.text)
        self.assertEqual(actions[0].min, day(0))
        json.dumps(card.as_json_dict(), ensure_ascii=False)

    def test_group_only_answers_keywords(self):
        bot.handle_message(msg(GROUP, "今天好熱"))
        self.assertEqual(self.api.replies, [])
        bot.handle_message(msg(GROUP, "隊長"))
        self.assertEqual(len(self.api.replies), 1)

    def test_show_postback_from_rich_menu(self):
        bot.handle_postback(pb(USER, {"action": "show"}))
        self.assertIn("隊長的餵藥日期", self.api.replies[-1][1].text)

    def test_card_shows_date_and_waiting_buttons(self):
        self.set_med("heartgard", 5)
        card = bot.status_card(bot.get_state())
        self.assertIn(day(5).replace("-", "/"), card.text)
        self.assertEqual(card.quick_reply.items[0].action.initial, day(5))
        self.assertEqual(len(card.quick_reply.items), 2)  # 沒有等待確認的藥
        self.set_med("bravecto", 0, "waiting")
        types = [i.action.type for i in bot.status_card(bot.get_state()).quick_reply.items]
        self.assertEqual(types, ["datetimepicker", "datetimepicker", "postback", "postback"])

    def test_past_initial_date_is_clamped_to_today(self):
        self.set_med("heartgard", -10)
        self.assertEqual(bot.status_card(bot.get_state()).quick_reply.items[0].action.initial, day(0))


class SetDate(BotTestCase):
    def test_past_date_rejected(self):
        bot.handle_postback(pb(GROUP, {"action": "set_date", "type": "heartgard"}, {"date": day(-1)}))
        self.assertIn("過去", self.api.replies[-1][1].text)
        self.assertIsNone(bot.get_state().heartgard_date)

    def test_future_date_saved_and_broadcast_excluding_origin(self):
        bot.register_user("U1")
        bot.register_user("Cg1")
        bot.handle_postback(pb(GROUP, {"action": "set_date", "type": "heartgard"}, {"date": day(5)}))
        self.assertEqual(bot.get_state().heartgard_date.date().isoformat(), day(5))
        self.assertEqual([to for to, _ in self.api.pushes], ["U1"])
        self.assertIn("小明", self.api.pushes[0][1].text)


class DoneAndDelay(BotTestCase):
    def test_done_broadcasts_once(self):
        bot.register_user("U1")
        bot.register_user("Cg1")
        self.set_med("bravecto", 0, "waiting")
        ev = pb(GROUP, {"action": "done_medicine", "type": "bravecto"})
        bot.handle_postback(ev)
        self.assertEqual([to for to, _ in self.api.pushes], ["U1"])
        self.assertEqual(bot.get_state().bravecto_date.date().isoformat(), day(90))
        self.api.pushes.clear()
        bot.handle_postback(ev)  # 第二個人再按：不重複廣播
        self.assertEqual(self.api.pushes, [])
        self.assertIn("已經完成過", self.api.replies[-1][1].text)

    def test_delay_broadcasts_to_other_chats_only(self):
        bot.register_user("U1")
        bot.register_user("Cg1")
        self.set_med("heartgard", 0, "waiting")
        bot.handle_postback(pb(USER, {"action": "delay_medicine", "type": "heartgard"}))
        self.assertEqual([to for to, _ in self.api.pushes], ["Cg1"])
        self.assertIn("阿姨", self.api.pushes[0][1].text)
        self.assertEqual(bot.get_state().heartgard_status, "pending")

    def test_reply_keeps_other_waiting_med_buttons(self):
        self.set_med("heartgard", 0, "waiting")
        self.set_med("bravecto", 0, "waiting")
        bot.handle_postback(pb(USER, {"action": "done_medicine", "type": "heartgard"}))
        items = self.api.replies[-1][1].quick_reply.items
        self.assertEqual([json.loads(i.action.data)["type"] for i in items], ["bravecto"] * 2)
        bot.handle_postback(pb(USER, {"action": "done_medicine", "type": "bravecto"}))
        self.assertIsNone(self.api.replies[-1][1].quick_reply)


class DailyReminder(BotTestCase):
    def test_quick_reply_for_users_card_for_groups_and_marks_waiting(self):
        bot.register_user("U1")
        bot.register_user("Cg9")
        bot.register_user("R5")
        self.set_med("heartgard", 0)
        bot.check_reminder()
        sent = dict(self.api.pushes)
        self.assertEqual([i.action.type for i in sent["U1"].quick_reply.items], ["postback"] * 2)
        for gid in ("Cg9", "R5"):
            self.assertEqual(sent[gid].type, "template")
            self.assertEqual([a.type for a in sent[gid].template.actions], ["postback"] * 2)
            json.dumps(sent[gid].as_json_dict(), ensure_ascii=False)
        self.assertEqual(bot.get_state().heartgard_status, "waiting")

    def test_both_meds_due_same_day_sends_one_message_with_four_buttons(self):
        bot.register_user("U1")
        bot.register_user("Cg9")
        self.set_med("heartgard", 0)
        self.set_med("bravecto", 0)
        bot.check_reminder()
        sent = dict(self.api.pushes)
        # one message per user, not two
        self.assertEqual(len(self.api.pushes), 2)
        self.assertEqual([i.action.type for i in sent["U1"].quick_reply.items], ["postback"] * 4)
        self.assertEqual([a.type for a in sent["Cg9"].template.actions], ["postback"] * 4)
        state = bot.get_state()
        self.assertEqual(state.heartgard_status, "waiting")
        self.assertEqual(state.bravecto_status, "waiting")

    def test_not_due_or_already_waiting_sends_nothing(self):
        bot.register_user("U1")
        self.set_med("heartgard", 3)
        self.set_med("bravecto", 0, "waiting")
        bot.check_reminder()
        self.assertEqual(self.api.pushes, [])


class RichMenu(unittest.TestCase):
    def test_menu_spec(self):
        menu = setup_rich_menu.build_rich_menu().as_json_dict()
        self.assertLessEqual(len(menu["chatBarText"]), 14)  # LINE 文件：chatBarText 最多 14 字
        self.assertEqual(len(menu["areas"]), 1)
        area = menu["areas"][0]
        self.assertEqual(json.loads(area["action"]["data"]), {"action": "show"})
        self.assertNotIn("displayText", area["action"])
        self.assertEqual((area["bounds"]["width"], area["bounds"]["height"]), (menu["size"]["width"], menu["size"]["height"]))

    def test_image_matches_menu_size(self):
        with open(setup_rich_menu.IMAGE, "rb") as f:
            data = f.read()
        self.assertEqual(data[:2], b"\xff\xd8")  # JPEG
        self.assertEqual(jpeg_size(data), setup_rich_menu.SIZE)
        self.assertLess(len(data), 1_000_000)


if __name__ == "__main__":
    unittest.main()
