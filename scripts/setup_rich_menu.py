"""一次性設定底部常駐選單（Rich Menu）：點一下就叫出狀態卡。重複執行會替換同名舊選單。"""
import json
import os

from dotenv import load_dotenv
from linebot import LineBotApi
from linebot.models import PostbackAction, RichMenu, RichMenuArea, RichMenuBounds, RichMenuSize

NAME = "captain-status"
IMAGE = os.path.join(os.path.dirname(__file__), "..", "assets", "rich_menu.jpg")
SIZE = (2500, 843)


def build_rich_menu() -> RichMenu:
    return RichMenu(
        size=RichMenuSize(width=SIZE[0], height=SIZE[1]),
        selected=True,
        name=NAME,
        chat_bar_text="隊長餵藥",
        areas=[RichMenuArea(
            bounds=RichMenuBounds(x=0, y=0, width=SIZE[0], height=SIZE[1]),
            action=PostbackAction(data=json.dumps({"action": "show"})))],
    )


def main():
    load_dotenv()
    api = LineBotApi(os.environ["LINE_CHANNEL_ACCESS_TOKEN"])
    old_ids = [m.rich_menu_id for m in api.get_rich_menu_list() if m.name == NAME]
    new_id = api.create_rich_menu(build_rich_menu())
    with open(IMAGE, "rb") as f:
        api.set_rich_menu_image(new_id, "image/jpeg", f)
    api.set_default_rich_menu(new_id)
    for old in old_ids:  # 新選單生效後才刪舊的，避免空窗
        api.delete_rich_menu(old)
    print("rich menu set:", new_id)


if __name__ == "__main__":
    main()
