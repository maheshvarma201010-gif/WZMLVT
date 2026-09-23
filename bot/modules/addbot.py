from asyncio import sleep
from html import escape
from time import time
from pyrogram.enums import ChatType, ButtonStyle
from pyrogram.filters import create
from pyrogram.handlers import MessageHandler
from pyrogram import Client

from .. import user_data
from ..core.config_manager import Config
from ..core.tg_client import TgClient
from ..helper.ext_utils.bot_utils import new_task, update_user_ldata
from ..helper.ext_utils.db_handler import database
from ..helper.telegram_helper.bot_commands import BotCommands
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import (
    delete_message,
    edit_message,
    send_message,
)

_ADD_BOT_HANDLER_DICT = {}
_USER_BOT_CLIENTS = {}


async def get_user_addbot_clients(user_id):
    user_dict = user_data.get(user_id, {})
    bots = user_dict.get("ADD_BOTS", [])
    if not bots:
        return []

    clients = []
    for idx, b_info in enumerate(bots):
        token = b_info.get("token")
        if not token:
            continue
        if token in _USER_BOT_CLIENTS:
            cl = _USER_BOT_CLIENTS[token]
            if cl.is_connected:
                clients.append(cl)
                continue
            else:
                _USER_BOT_CLIENTS.pop(token, None)
        try:
            cl = TgClient.wztgClient(
                f"addbot_{user_id}_{idx}_{int(time())}",
                bot_token=token,
                no_updates=True,
            )
            await cl.start()
            _USER_BOT_CLIENTS[token] = cl
            clients.append(cl)
        except Exception as e:
            pass

    return clients


def get_addbot_menu(user_id):
    user_dict = user_data.get(user_id, {})
    bots = user_dict.get("ADD_BOTS", [])

    text = "<b>🤖 Configured Upload Bots (ADDBOT)</b>\n\n"
    if bots:
        text += "<b>Active Bots:</b>\n"
        for idx, b in enumerate(bots, start=1):
            uname = b.get("username", "UnknownBot")
            text += f"{idx}. @{escape(uname)} (ID: <code>{b.get('id', 'N/A')}</code>)\n"
        text += f"\n<i>Total: {len(bots)}/5 bots added</i>"
    else:
        text += "<i>No custom bot tokens configured.</i>\nUploads will be sent directly to your DM."

    text += "\n\n<i>Note: You can add up to 5 custom bot tokens to route uploads through your own bots.</i>"

    buttons = ButtonMaker()
    if len(bots) < 5:
        buttons.data_button("➕ Add Bot", f"addbot add {user_id}", position="header")
    if bots:
        buttons.data_button("🗑️ Delete Bot", f"addbot del_menu {user_id}", position="header")
    buttons.data_button("❌ Close", f"addbot close {user_id}", position="footer", style=ButtonStyle.DANGER)

    return text, buttons.build_menu(2)


@new_task
async def addbot_command(client, message):
    if message.chat.type != ChatType.PRIVATE:
        addbot_cmd = (
            f"/{BotCommands.AddBotCommand[0]}"
            if isinstance(BotCommands.AddBotCommand, list)
            else f"/{BotCommands.AddBotCommand}"
        )
        await send_message(
            message,
            f"<blockquote><b>⚠️ Security Warning:</b> The <code>{addbot_cmd}</code> command is restricted to <b>Private DM</b> only to prevent bot token leaks in group chats.</blockquote>",
        )
        return

    user_id = message.from_user.id if message.from_user else message.chat.id
    text, buttons = get_addbot_menu(user_id)
    await send_message(message, text, buttons)


@new_task
async def addbot_callback(client, query):
    user_id = query.from_user.id
    data = query.data.split()
    cmd = data[1]
    target_user_id = int(data[2]) if len(data) > 2 else user_id

    if user_id != target_user_id:
        return await query.answer("This menu is not for you!", show_alert=True)

    user_dict = user_data.get(user_id, {})
    bots = user_dict.get("ADD_BOTS", [])

    if cmd == "close":
        await query.answer()
        await delete_message(query.message)
    elif cmd == "add":
        if len(bots) >= 5:
            return await query.answer("You can add up to 5 bot tokens maximum!", show_alert=True)

        await query.answer()
        buttons = ButtonMaker()
        buttons.data_button("◀️ Back", f"addbot main {user_id}", position="footer")
        prompt_msg = await edit_message(
            query.message,
            "<b>➕ Add Custom Bot Token:</b>\n\n"
            "Please send your Telegram Bot Token from @BotFather.\n"
            "Example: <code>1234567890:ABCdefGHIjklMNOpqrsTUVwxyZ</code>\n\n"
            "⏱️ <i>Timeout: 60 seconds</i>",
            buttons.build_menu(1),
        )

        event_done = TgClient.bot.loop.create_future()
        token_input = []

        async def token_filter(_, __, event):
            u = event.from_user or event.sender_chat
            return bool(u and u.id == user_id and event.chat.id == query.message.chat.id and event.text)

        async def token_handler(_, msg):
            token_input.append(msg.text.strip())
            await delete_message(msg)
            if not event_done.done():
                event_done.set_result(True)

        from asyncio import wait_for
        h = client.add_handler(MessageHandler(token_handler, filters=create(token_filter)), group=-1)

        try:
            await wait_for(event_done, timeout=60)
            if token_input:
                bot_token = token_input[0]
                status_msg = await send_message(query.message, "<b>Testing bot token...</b>")
                try:
                    test_client = Client(
                        name=f"addbot_test_{user_id}_{int(time())}",
                        api_id=Config.TELEGRAM_API,
                        api_hash=Config.TELEGRAM_HASH,
                        bot_token=bot_token,
                        in_memory=True,
                    )
                    await test_client.start()
                    bot_me = await test_client.get_me()
                    await test_client.stop()

                    new_bot = {
                        "token": bot_token,
                        "username": bot_me.username,
                        "id": bot_me.id,
                    }

                    if not isinstance(bots, list):
                        bots = []

                    # Avoid duplicate token
                    bots = [b for b in bots if b.get("token") != bot_token]
                    bots.append(new_bot)

                    update_user_ldata(user_id, "ADD_BOTS", bots)
                    await database.update_user_data(user_id)

                    await edit_message(
                        status_msg,
                        f"<b>✅ Bot Added Successfully!</b>\n\n"
                        f"• <b>Username:</b> @{escape(bot_me.username)}\n"
                        f"• <b>Bot ID:</b> <code>{bot_me.id}</code>",
                    )
                except Exception as e:
                    await edit_message(status_msg, f"<b>❌ Invalid Bot Token:</b> {escape(str(e))}")
        except Exception:
            pass
        finally:
            client.remove_handler(*h)
            text, btns = get_addbot_menu(user_id)
            await edit_message(query.message, text, btns)

    elif cmd == "del_menu":
        await query.answer()
        buttons = ButtonMaker()
        for idx, b in enumerate(bots):
            uname = b.get("username", f"Bot #{idx+1}")
            buttons.data_button(f"❌ Delete @{uname}", f"addbot do_del_{idx} {user_id}")
        buttons.data_button("◀️ Back", f"addbot main {user_id}", position="footer")
        await edit_message(query.message, "<b>🗑️ Select Bot Token to Delete:</b>", buttons.build_menu(1))

    elif cmd.startswith("do_del_"):
        await query.answer()
        try:
            idx = int(cmd.replace("do_del_", ""))
            if 0 <= idx < len(bots):
                removed = bots.pop(idx)
                update_user_ldata(user_id, "ADD_BOTS", bots)
                await database.update_user_data(user_id)
                await query.answer(f"Deleted @{removed.get('username', 'Bot')}!", show_alert=True)
        except Exception as e:
            await query.answer(f"Failed to delete: {e}", show_alert=True)

        text, btns = get_addbot_menu(user_id)
        await edit_message(query.message, text, btns)

    elif cmd == "main":
        await query.answer()
        text, btns = get_addbot_menu(user_id)
        await edit_message(query.message, text, btns)
