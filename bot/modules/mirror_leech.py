from ast import literal_eval
from asyncio import sleep
from base64 import b64encode
from html import escape
from os import path as ospath
from re import match as re_match
from time import time

from aiofiles import open as aiopen
from aiofiles.os import path as aiopath
from bot.core.config_manager import Config

from .. import DOWNLOAD_DIR, LOGGER, bot_loop, task_dict_lock, user_data
from ..core.seedr_client import SeedrClient
from ..helper.ext_utils.bot_utils import (
    COMMAND_USAGE,
    arg_parser,
    get_content_type,
    new_task,
    sync_to_async,
)
from ..helper.ext_utils.status_utils import (
    get_readable_file_size,
    get_readable_time,
)
from ..helper.ext_utils.exceptions import DirectDownloadLinkException
from ..helper.ext_utils.links_utils import (
    is_gdrive_id,
    is_gdrive_link,
    is_mega_link,
    is_magnet,
    is_rclone_path,
    is_telegram_link,
    is_url,
)
from ..helper.ext_utils.task_manager import pre_task_check
from ..helper.listeners.task_listener import TaskListener
from ..helper.mirror_leech_utils.download_utils.alldebrid_resolver import (
    alldebrid_resolve,
    alldebrid_resolve_magnet,
    alldebrid_resolve_torrent,
)
from ..helper.mirror_leech_utils.download_utils.aria2_download import (
    add_aria2_download,
)
from ..helper.mirror_leech_utils.download_utils.direct_downloader import (
    add_direct_download,
)
from ..helper.mirror_leech_utils.download_utils.direct_link_generator import (
    direct_link_generator,
)
from ..helper.mirror_leech_utils.download_utils.gd_download import add_gd_download
from ..helper.mirror_leech_utils.download_utils.jd_download import add_jd_download
from ..helper.mirror_leech_utils.download_utils.mega_download import add_mega_download
from ..helper.mirror_leech_utils.download_utils.nzb_downloader import add_nzb
from ..helper.mirror_leech_utils.download_utils.qbit_download import add_qb_torrent
from ..helper.mirror_leech_utils.download_utils.rclone_download import (
    add_rclone_download,
)
from ..helper.mirror_leech_utils.download_utils.seedr_download import (
    _build_contents,
    _delete_seedr_folder,
    _match_folder,
    add_seedr_download,
)
from ..helper.mirror_leech_utils.download_utils.telegram_download import (
    TelegramDownloadHelper,
)
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.bot_commands import BotCommands
from ..helper.telegram_helper.message_utils import (
    auto_delete_message,
    delete_links,
    edit_message,
    get_tg_link_message,
    send_message,
)

ht_tasks = {}
ht_lock = bot_loop.create_task if False else None


class Mirror(TaskListener):
    def __init__(
        self,
        client,
        message,
        is_qbit=False,
        is_leech=False,
        is_jd=False,
        is_nzb=False,
        is_seedr=False,
        is_uphoster=False,
        same_dir=None,
        bulk=None,
        multi_tag=None,
        options="",
        **kwargs,
    ):
        if same_dir is None:
            same_dir = {}
        if bulk is None:
            bulk = []
        self.message = message
        self.client = client
        self.multi_tag = multi_tag
        self.options = options
        self.same_dir = same_dir
        self.bulk = bulk
        super().__init__()
        self.is_qbit = is_qbit
        self.is_leech = is_leech
        self.is_jd = is_jd
        self.is_nzb = is_nzb
        self.is_seedr = is_seedr
        self.is_uphoster = is_uphoster

    async def new_event(self):
        if self.is_leech:
            if Config.DISABLE_LEECH:
                await send_message(
                    self.message, "<blockquote>The Leech command is currently disabled.</blockquote>"
                )
                return
        elif Config.DISABLE_MIRROR and not self.is_uphoster:
            await send_message(
                self.message, "<blockquote>The Mirror command is currently disabled.</blockquote>"
            )
            return
        text = self.message.text.split("\n")
        input_list = text[0].split(" ")

        check_msg, check_button = await pre_task_check(self.message)
        if check_msg:
            await delete_links(self.message)
            await auto_delete_message(
                await send_message(self.message, check_msg, check_button)
            )
            return

        args = {
            "-doc": False,
            "-med": False,
            "-d": False,
            "-j": False,
            "-s": False,
            "-b": False,
            "-e": False,
            "-z": False,
            "-sv": False,
            "-ss": False,
            "-f": False,
            "-fd": False,
            "-fu": False,
            "-hl": False,
            "-bt": False,
            "-ut": False,
            "-ad": False,
            "-yt": False,
            "-seedr": False,
            "-ht": False,
            "-i": 0,
            "-sp": 0,
            "link": "",
            "-n": "",
            "-m": "",
            "-meta": "",
            "-up": "",
            "-ud": "",
            "-gc": "",
            "-rcf": "",
            "-au": "",
            "-ap": "",
            "-h": "",
            "-t": "",
            "-ca": "",
            "-cv": "",
            "-ns": "",
            "-tl": "",
            "-ff": set(),
        }

        arg_parser(input_list[1:], args)

        if Config.DISABLE_BULK and args.get("-b", False):
            await send_message(self.message, "<blockquote>Bulk downloads are currently disabled.</blockquote>")
            return

        if Config.DISABLE_MULTI and int(args.get("-i", 1)) > 1:
            await send_message(
                self.message,
                "<blockquote>Multi-downloads are currently disabled.</blockquote>",
            )
            return

        if Config.DISABLE_SEED and args.get("-d", False):
            await send_message(
                self.message,
                "<blockquote>Seeding is currently disabled.</blockquote>",
            )
            return

        if Config.DISABLE_FF_MODE and args.get("-ff"):
            await send_message(self.message, "<blockquote>FFmpeg commands are currently disabled.</blockquote>")
            return

        self.select = args["-s"]
        self.seed = args["-d"]
        self.name = args["-n"]
        self.up_dest = args["-up"]
        self.dump_dest = args["-ud"]
        self.category = args["-gc"]
        self.rc_flags = args["-rcf"]
        self.link = args["link"]
        self.compress = args["-z"]
        self.extract = args["-e"]
        self.join = args["-j"]
        self.thumb = args["-t"]
        self.split_size = args["-sp"]
        self.sample_video = args["-sv"]
        self.screen_shots = args["-ss"]
        self.force_run = args["-f"]
        self.force_download = args["-fd"]
        self.force_upload = args["-fu"]
        self.convert_audio = args["-ca"]
        self.convert_video = args["-cv"]
        self.name_swap = args["-ns"]
        self.hybrid_leech = args["-hl"]
        self.thumbnail_layout = args["-tl"]
        self.as_doc = args["-doc"]
        self.as_med = args["-med"]
        self.folder_name = f"/{args['-m']}".rstrip("/") if len(args["-m"]) > 0 else ""
        self.bot_trans = args["-bt"]
        self.user_trans = args["-ut"]
        self.is_alldebrid = args["-ad"]
        self.is_seedr = args["-seedr"] or self.is_seedr
        self.is_yt = args["-yt"]
        self.ht_flag = args["-ht"]

        if self.is_seedr and not await seedr_guard(self.message, self.user_id):
            return

        self.metadata_dict = self.default_metadata_dict.copy()
        self.audio_metadata_dict = self.audio_metadata_dict.copy()
        self.video_metadata_dict = self.video_metadata_dict.copy()
        self.subtitle_metadata_dict = self.subtitle_metadata_dict.copy()
        if args["-meta"]:
            meta = self.metadata_processor.parse_string(args["-meta"])
            self.metadata_dict = self.metadata_processor.merge_dicts(
                self.metadata_dict, meta
            )

        headers = args["-h"]
        is_bulk = args["-b"]

        bulk_start = 0
        bulk_end = 0
        ratio = None
        seed_time = None
        reply_to = None
        file_ = None
        session = ""

        try:
            self.multi = int(args["-i"])
        except Exception:
            self.multi = 0

        try:
            if args["-ff"]:
                if isinstance(args["-ff"], set):
                    self.ffmpeg_cmds = args["-ff"]
                else:
                    value = literal_eval(args["-ff"])
                    if not isinstance(value, (dict, set, list, tuple)):
                        raise ValueError("ffmpeg_cmds must be a dict/set/list/tuple")
                    self.ffmpeg_cmds = value
        except Exception as e:
            self.ffmpeg_cmds = None
            LOGGER.error(e)

        if not isinstance(self.seed, bool):
            dargs = self.seed.split(":")
            ratio = dargs[0] or None
            if len(dargs) == 2:
                seed_time = dargs[1] or None
            self.seed = True

        if not isinstance(is_bulk, bool):
            dargs = is_bulk.split(":")
            bulk_start = dargs[0] or 0
            if len(dargs) == 2:
                bulk_end = dargs[1] or 0
            is_bulk = True

        if not is_bulk:
            if self.multi > 0:
                if self.folder_name:
                    async with task_dict_lock:
                        if self.folder_name in self.same_dir:
                            self.same_dir[self.folder_name]["tasks"].add(self.mid)
                            for fd_name in self.same_dir:
                                if fd_name != self.folder_name:
                                    self.same_dir[fd_name]["total"] -= 1
                        elif self.same_dir:
                            self.same_dir[self.folder_name] = {
                                "total": self.multi,
                                "tasks": {self.mid},
                            }
                            for fd_name in self.same_dir:
                                if fd_name != self.folder_name:
                                    self.same_dir[fd_name]["total"] -= 1
                        else:
                            self.same_dir = {
                                self.folder_name: {
                                    "total": self.multi,
                                    "tasks": {self.mid},
                                }
                            }
                elif self.same_dir:
                    async with task_dict_lock:
                        for fd_name in self.same_dir:
                            self.same_dir[fd_name]["total"] -= 1
        else:
            await self.init_bulk(input_list, bulk_start, bulk_end, Mirror)
            return

        if len(self.bulk) != 0:
            del self.bulk[0]

        await self.run_multi(input_list, Mirror)

        await self.get_tag(text)

        if self.ht_flag:
            user_id = self.user_id
            event_done = bot_loop.create_future()
            ht_tasks[self.mid] = {
                "merge": False,
                "user_id": user_id,
                "future": event_done,
            }
            buttons = ButtonMaker()
            buttons.data_button("Merge: OFF", f"htmerge merge {self.mid}")
            buttons.data_button("Done", f"htmerge done {self.mid}")
            prompt_msg = await send_message(
                self.message,
                f"<b>Task Received with -ht flag.</b>\nChoose whether to merge files before uploading:",
                buttons.build_menu(2),
            )
            try:
                await event_done
            except Exception:
                pass
            self.manual_merge = ht_tasks.get(self.mid, {}).get("merge", False)
            ht_tasks.pop(self.mid, None)
            await delete_message(prompt_msg)

        path = f"{DOWNLOAD_DIR}{self.mid}{self.folder_name}"

        if not self.link and (reply_to := self.message.reply_to_message):
            if reply_to.text:
                self.link = reply_to.text.split("\n", 1)[0].strip()
        if is_telegram_link(self.link):
            try:
                reply_to, session = await get_tg_link_message(self.link)
            except Exception as e:
                await send_message(self.message, f"ERROR: {e}")
                await self.remove_from_same_dir()
                await delete_links(self.message)
                return

        if isinstance(reply_to, list):
            self.bulk = reply_to
            b_msg = input_list[:1]
            self.options = " ".join(input_list[1:])
            b_msg.append(f"{self.bulk[0]} -i {len(self.bulk)} {self.options}")
            nextmsg = await send_message(self.message, " ".join(b_msg))
            nextmsg = await self.client.get_messages(
                chat_id=self.message.chat.id, message_ids=nextmsg.id
            )
            if self.message.from_user:
                nextmsg.from_user = self.user
            else:
                nextmsg.sender_chat = self.user
            await Mirror(
                self.client,
                nextmsg,
                self.is_qbit,
                self.is_leech,
                self.is_jd,
                self.is_nzb,
                self.is_seedr,
                self.is_uphoster,
                self.same_dir,
                self.bulk,
                self.multi_tag,
                self.options,
            ).new_event()
            return

        if reply_to:
            file_ = (
                reply_to.document
                or reply_to.photo
                or reply_to.video
                or reply_to.audio
                or reply_to.voice
                or reply_to.video_note
                or reply_to.sticker
                or reply_to.animation
                or None
            )
            self.file_details = {"caption": reply_to.caption}

            if file_ is None:
                if reply_text := reply_to.text:
                    self.link = reply_text.split("\n", 1)[0].strip()
                else:
                    reply_to = None
            elif reply_to.document and (
                file_.mime_type == "application/x-bittorrent"
                or file_.file_name.endswith((".torrent", ".dlc", ".nzb"))
            ):
                self.link = await reply_to.download()
                file_ = None

        if (
            not self.link
            and file_ is None
            or is_telegram_link(self.link)
            and reply_to is None
            or file_ is None
            and not is_url(self.link)
            and not is_magnet(self.link)
            and not await aiopath.exists(self.link)
            and not is_rclone_path(self.link)
            and not is_gdrive_id(self.link)
            and not is_gdrive_link(self.link)
            and not is_mega_link(self.link)
        ):
            await send_message(
                self.message, COMMAND_USAGE["mirror"][0], COMMAND_USAGE["mirror"][1]
            )
            await self.remove_from_same_dir()
            await delete_links(self.message)
            return

        if len(self.link) > 0:
            LOGGER.info(self.link)

        try:
            await self.before_start()
        except Exception as e:
            await send_message(self.message, e)
            await self.remove_from_same_dir()
            await delete_links(self.message)
            return

        self._set_mode_engine()

        if self.is_alldebrid and (
            is_magnet(self.link) or self.link.endswith(".torrent")
        ):
            try:
                if is_magnet(self.link):
                    LOGGER.info("AllDebrid magnet route")
                    resolved = await alldebrid_resolve_magnet(
                        self.link,
                        is_cancelled=lambda: self.is_cancelled,
                    )
                else:
                    LOGGER.info(f"AllDebrid torrent file route: {self.link}")
                    async with aiopen(self.link, "rb") as fh:
                        torrent_bytes = await fh.read()
                    resolved = await alldebrid_resolve_torrent(
                        torrent_bytes,
                        ospath.basename(self.link),
                        is_cancelled=lambda: self.is_cancelled,
                    )
            except DirectDownloadLinkException as e:
                e = str(e)
                LOGGER.info(e)
                if e.startswith("ERROR:"):
                    await send_message(self.message, e)
                    await self.remove_from_same_dir()
                    await delete_links(self.message)
                    return
                resolved = None
            except Exception as e:
                await send_message(self.message, e)
                await self.remove_from_same_dir()
                await delete_links(self.message)
                return
            if isinstance(resolved, dict):
                self._alldebrid_magnet_id = resolved.get("magnet_id", 0)
                self.link = resolved
                self.is_jd = False
                self.is_qbit = False

        if (
            isinstance(self.link, str)
            and not self.is_jd
            and not self.is_nzb
            and not self.is_seedr
            and not self.is_qbit
            and not is_magnet(self.link)
            and not is_rclone_path(self.link)
            and not is_gdrive_link(self.link)
            and not self.link.endswith(".torrent")
            and file_ is None
            and not is_gdrive_id(self.link)
            and not is_mega_link(self.link)
        ):
            if self.is_alldebrid:
                try:
                    self.link = await alldebrid_resolve(self.link)
                    if isinstance(self.link, str):
                        LOGGER.info(f"AllDebrid link: {self.link}")
                except DirectDownloadLinkException as e:
                    e = str(e)
                    LOGGER.info(e)
                    if e.startswith("ERROR:"):
                        await send_message(self.message, e)
                        await self.remove_from_same_dir()
                        await delete_links(self.message)
                        return
                except Exception as e:
                    await send_message(self.message, e)
                    await self.remove_from_same_dir()
                    await delete_links(self.message)
                    return

            if isinstance(self.link, str) and (
                (content_type := await get_content_type(self.link)) is None
                or re_match(r"text/html|text/plain", content_type)
            ):
                try:
                    self.link = await sync_to_async(direct_link_generator, self.link)
                    if isinstance(self.link, tuple):
                        self.link, headers = self.link
                    elif isinstance(self.link, str):
                        LOGGER.info(f"Generated link: {self.link}")
                except DirectDownloadLinkException as e:
                    e = str(e)
                    if "This link requires a password!" not in e:
                        LOGGER.info(e)
                    if e.startswith("ERROR:"):
                        await send_message(self.message, e)
                        await self.remove_from_same_dir()
                        await delete_links(self.message)
                        return
                except Exception as e:
                    await send_message(self.message, e)
                    await self.remove_from_same_dir()
                    await delete_links(self.message)
                    return

        if file_ is not None:
            await TelegramDownloadHelper(self).add_download(
                reply_to, f"{path}/", session
            )
        elif isinstance(self.link, dict):
            await add_direct_download(self, path)
        elif self.is_jd:
            await add_jd_download(self, path)
        elif self.is_qbit:
            await add_qb_torrent(self, path, ratio, seed_time)
        elif self.is_nzb:
            await add_nzb(self, path)
        elif self.is_seedr:
            await add_seedr_download(self, path)
        elif is_rclone_path(self.link):
            await add_rclone_download(self, f"{path}/")
        elif is_gdrive_link(self.link) or is_gdrive_id(self.link):
            await add_gd_download(self, path)
        elif is_mega_link(self.link):
            await add_mega_download(self, f"{path}/")
        else:
            ussr = args["-au"]
            pssw = args["-ap"]
            if ussr or pssw:
                auth = f"{ussr}:{pssw}"
                headers += (
                    f" authorization: Basic {b64encode(auth.encode()).decode('ascii')}"
                )
            await add_aria2_download(self, path, headers, ratio, seed_time)


@new_task
async def ht_merge_callback(_, query):
    data = query.data.split()
    mid = int(data[2])
    task_info = ht_tasks.get(mid)
    if not task_info:
        return await query.answer("Task expired or already started!", show_alert=True)
    if query.from_user.id != task_info["user_id"]:
        return await query.answer("This menu is not for you!", show_alert=True)

    if data[1] == "merge":
        task_info["merge"] = not task_info["merge"]
        state_str = "ON" if task_info["merge"] else "OFF"
        await query.answer(f"Merge turned {state_str}")
        buttons = ButtonMaker()
        buttons.data_button(f"Merge: {state_str}", f"htmerge merge {mid}")
        buttons.data_button("Done", f"htmerge done {mid}")
        await edit_message(query.message, query.message.text.html, buttons.build_menu(2))
    elif data[1] == "done":
        await query.answer("Starting task...")
        fut = task_info.get("future")
        if fut and not fut.done():
            fut.set_result(True)


async def mirror(client, message):
    bot_loop.create_task(Mirror(client, message).new_event())


async def qb_mirror(client, message):
    bot_loop.create_task(Mirror(client, message, is_qbit=True).new_event())


async def jd_mirror(client, message):
    if Config.DISABLE_JD:
        await message.reply("JDownloader is currently disabled by the Bot Owner.")
        return
    bot_loop.create_task(Mirror(client, message, is_jd=True).new_event())


def hydra_nzb_id(message, cmd, force_extract=True):
    text_parts = message.text.split()
    if len(text_parts) > 1 and not text_parts[1].startswith(("http", "ftp", "/")):
        potential_id = text_parts[1]
        clean = potential_id.lstrip("-").replace("_", "")
        if clean.isalnum() and not (potential_id.startswith("-") and clean.isalpha()):
            nzb_url = f"{Config.HYDRA_IP.rstrip('/')}/getnzb/api/{potential_id}?apikey={Config.HYDRA_API_KEY}"
            extra = " ".join(text_parts[2:])
            message.text = f"{cmd} {nzb_url} -e {extra}".strip()
            return potential_id
    elif force_extract and "-e" not in message.text:
        message.text += " -e"
    return None


async def nzb_mirror(client, message):
    if Config.DISABLE_NZB:
        await message.reply("SABnzbd is currently disabled by the Bot Owner.")
        return
    nzb_id = hydra_nzb_id(message, "/nzbmirror")
    mirror_task = Mirror(client, message, is_nzb=True)
    if nzb_id:
        mirror_task.nzb_id = nzb_id
    bot_loop.create_task(mirror_task.new_event())


async def leech(client, message):
    bot_loop.create_task(Mirror(client, message, is_leech=True).new_event())


async def qb_leech(client, message):
    bot_loop.create_task(
        Mirror(client, message, is_qbit=True, is_leech=True).new_event()
    )


async def jd_leech(client, message):
    if Config.DISABLE_JD:
        await message.reply("JDownloader is currently disabled by the Bot Owner.")
        return
    bot_loop.create_task(Mirror(client, message, is_leech=True, is_jd=True).new_event())


async def nzb_leech(client, message):
    if Config.DISABLE_NZB:
        await message.reply("SABnzbd is currently disabled by the Bot Owner.")
        return
    nzb_id = hydra_nzb_id(message, "/nzbleech")
    mirror_task = Mirror(client, message, is_leech=True, is_nzb=True)
    if nzb_id:
        mirror_task.nzb_id = nzb_id
    bot_loop.create_task(mirror_task.new_event())


@new_task
async def merge_command(client, message):
    reply_to = message.reply_to_message
    if not reply_to:
        await send_message(
            message,
            "<blockquote>Reply to the first Telegram file or video in sequence to merge!</blockquote>",
        )
        return

    file_ = reply_to.video or reply_to.document
    if file_ is None:
        await send_message(
            message,
            "<blockquote>Unsupported media! Reply to first Telegram file or video in sequence.</blockquote>",
        )
        return

    text = message.text.split("\n")
    input_list = text[0].split(" ")
    args = {
        "-i": 0,
        "-n": "",
        "-up": "",
        "-sp": 0,
        "-doc": False,
        "-med": False,
    }
    arg_parser(input_list[1:], args)

    count = int(args["-i"]) if str(args["-i"]).isdigit() else 0
    if count <= 0:
        await send_message(
            message,
            "<blockquote>Specify file count using -i. Usage: <code>/merge -i 5 -n name.mkv</code></blockquote>",
        )
        return

    custom_name = args["-n"]
    if not custom_name:
        await send_message(
            message,
            "<blockquote>Specify custom output name using -n. Usage: <code>/merge -i 5 -n name.mkv</code></blockquote>",
        )
        return

    mirror_task = Mirror(client, message, is_leech=not bool(args["-up"]))
    mirror_task.name = custom_name
    mirror_task.up_dest = args["-up"]
    mirror_task.split_size = args["-sp"]
    mirror_task.as_doc = args["-doc"]
    mirror_task.as_med = args["-med"]
    mirror_task.manual_merge = True
    mirror_task.merge_custom_name = custom_name

    try:
        await mirror_task.before_start()
    except Exception as e:
        await send_message(message, str(e))
        return

    mirror_task._set_mode_engine()
    path = f"{DOWNLOAD_DIR}{mirror_task.mid}"
    await makedirs(path, exist_ok=True)

    start_id = reply_to.id
    chat_id = message.chat.id

    msg = await send_message(message, f"<b>Fetching {count} files for merge task...</b>")

    for i in range(count):
        curr_id = start_id + i
        try:
            curr_msg = await client.get_messages(chat_id, curr_id)
        except Exception as e:
            await edit_message(msg, f"Error fetching message #{curr_id}: {e}")
            await clean_download(path)
            return

        curr_file = curr_msg.video or curr_msg.document if curr_msg else None
        if not curr_file:
            await edit_message(
                msg,
                f"Message #{curr_id} is not a valid video or document! Sequence aborted.",
            )
            await clean_download(path)
            return

        idx_prefix = f"{i+1:04d}_"
        dl_helper = TelegramDownloadHelper(mirror_task)
        mirror_task.name = f"{idx_prefix}{curr_file.file_name or 'video.mkv'}"
        await dl_helper.add_download(curr_msg, f"{path}/", session="")

    await delete_message(msg)
    mirror_task.name = custom_name
    await mirror_task.on_download_complete()


async def uphoster(client, message):
    nzb_id = hydra_nzb_id(message, "/uphoster", force_extract=False)
    if nzb_id and Config.DISABLE_NZB:
        await message.reply("SABnzbd is currently disabled by the Bot Owner.")
        return
    mirror_task = Mirror(client, message, is_uphoster=True, is_nzb=bool(nzb_id))
    if nzb_id:
        mirror_task.nzb_id = nzb_id
    bot_loop.create_task(mirror_task.new_event())


async def clear_seedr_account(email, password):
    client = SeedrClient(email, password)
    await client.login()
    res = await client.list_contents("0")
    if not isinstance(res, dict):
        return 0, 0
    t_count = 0
    f_count = 0
    for t in res.get("torrents", []):
        t_id = t.get("id") or t.get("user_torrent_id")
        if t_id:
            try:
                await client.delete("torrent", t_id)
                t_count += 1
            except Exception:
                pass
    for f in res.get("folders", []):
        f_id = f.get("id")
        if f_id:
            try:
                await client.delete("folder", f_id)
                f_count += 1
            except Exception:
                pass
    return t_count, f_count


def _seedr_creds(user_id):
    user_dict = user_data.get(user_id, {})
    email = user_dict.get("SEEDR_EMAIL") or Config.SEEDR_EMAIL
    password = user_dict.get("SEEDR_PASSWORD") or Config.SEEDR_PASSWORD
    return email, password


async def seedr_guard(message, user_id):
    if Config.DISABLE_SEEDR:
        await send_message(message, "<blockquote>Seedr is currently disabled by the Bot Owner.</blockquote>")
        return False
    email, password = _seedr_creds(user_id)
    if not email or not password:
        uset_cmd = (
            f"/{BotCommands.UserSetCommand[0]}"
            if isinstance(BotCommands.UserSetCommand, list)
            else f"/{BotCommands.UserSetCommand}"
        )
        await send_message(
            message,
            f"<blockquote>Seedr credentials not configured! Please set SEEDR_EMAIL and SEEDR_PASSWORD in {uset_cmd} or bot settings.</blockquote>",
        )
        return False
    return True


@new_task
async def seedr_link(client, message):
    user_id = message.from_user.id
    if not await seedr_guard(message, user_id):
        return
    email, password = _seedr_creds(user_id)
    tag = message.from_user.mention(style="html") if message.from_user else "N/A"
    seedrlink_cmd = (
        f"/{BotCommands.SeedrLinkCommand[0]}"
        if isinstance(BotCommands.SeedrLinkCommand, list)
        else f"/{BotCommands.SeedrLinkCommand}"
    )

    link = ""
    args = message.text.split(maxsplit=1)
    if len(args) > 1:
        link = args[1].strip()
    elif reply_to := message.reply_to_message:
        if reply_to.text:
            link = reply_to.text.split("\n", 1)[0].strip()

    if not link or not (is_magnet(link) or is_url(link) or link.endswith(".torrent")):
        await message.reply(
            f"<blockquote>Please provide a valid magnet link or .torrent URL!\nUsage: <code>{seedrlink_cmd} magnet:...</code></blockquote>"
        )
        return

    msg = await send_message(message, "<b>Processing Seedr magnet link...</b>")
    seedr_client = SeedrClient(email, password)
    torrent_id = None
    folder_id = None

    try:
        await seedr_client.login()
        log_link = f"{link[:60]}..." if is_magnet(link) else link
        LOGGER.info(f"SeedrLink: Adding magnet: {log_link}")
        result = await seedr_client.add_torrent(link)
        torrent_id = result.get("torrent_id") or result.get("user_torrent_id")
        title = result.get("title") or ""

        if not torrent_id:
            raise ValueError("Failed to obtain Seedr torrent ID!")

        if title:
            await edit_message(
                msg,
                f"<b>🌱 Added to Seedr Cloud</b>\n\n<blockquote>• <b>Title:</b> <code>{escape(title)}</code>\nFetching cloud progress...</blockquote>",
            )

        known_folders = {
            f.get("id")
            for f in (await seedr_client.list_contents("0")).get("folders", [])
        }
        folder_names = {title} if title else set()
        not_found_count = 0
        last_progress = ""
        last_prog_value = -1.0
        stall_count = 0

        while True:
            await sleep(3)
            stall_count += 1
            if stall_count >= 400:
                raise ValueError("Seedr cloud download stalled with no progress!")
            res = await seedr_client.list_contents("0")
            torrent = next(
                (
                    t
                    for t in res.get("torrents", [])
                    if t.get("id") == torrent_id
                    or t.get("user_torrent_id") == torrent_id
                ),
                None,
            )
            if torrent is not None:
                not_found_count = 0
                prog = float(torrent.get("progress", 0) or 0)
                if prog != last_prog_value:
                    last_prog_value = prog
                    stall_count = 0
                name_str = torrent.get("name") or title or "Torrent"
                if torrent.get("name"):
                    folder_names.add(torrent["name"])
                prog_str = f"<b>🌱 Seedr Cloud Downloading...</b>\n\n<blockquote>• <b>Name:</b> <code>{escape(name_str)}</code>\n• <b>Progress:</b> <code>{round(prog, 2)}%</code></blockquote>"
                if prog_str != last_progress:
                    last_progress = prog_str
                    await edit_message(msg, prog_str)

            folder = _match_folder(
                res.get("folders", []),
                folder_names,
                known_folders,
                torrent is None,
            )

            if folder is not None:
                folder_contents = await seedr_client.list_contents(folder["id"])
                if folder_contents.get("files") or folder_contents.get("folders"):
                    folder_id = folder["id"]
                    break
            else:
                not_found_count += 1
                if not_found_count >= 36:
                    raise ValueError("Torrent not found on Seedr account!")

        await edit_message(msg, "<b>Generating Seedr direct download links...</b>")
        contents, total_size = await _build_contents(seedr_client, folder_id)
        if not contents:
            raise ValueError("No downloadable files found in Seedr folder!")

        buttons = ButtonMaker()
        text_lines = [
            f"<b>🌱 {escape(title or contents[0]['filename'])}</b>\n",
            f"<blockquote>• <b>Task Size:</b> {get_readable_file_size(total_size)}",
            f"• <b>Time Elapsed:</b> {get_readable_time(time() - message.date.timestamp())}",
            "• <b>In Mode:</b> Seedr Cloud",
            f"• <b>Total Files:</b> {len(contents)}",
            f"• <b>User:</b> {tag}</blockquote>\n",
            "<b>📁 Direct Download Files:</b>",
        ]

        for idx, item in enumerate(contents, start=1):
            fname = item["filename"]
            furl = item["url"]
            fsize = get_readable_file_size(item["size"])
            text_lines.append(
                f"{idx}. <a href='{furl}'>{escape(fname)}</a> (<code>{fsize}</code>)"
            )
            buttons.url_button(f"Download #{idx}", furl)

        out_text = "\n".join(text_lines)
        if len(out_text) > 4000:
            out_text = (
                out_text[:3900]
                + "\n\n<i>(Links truncated due to length limits. Use buttons below)</i>"
            )

        await edit_message(msg, out_text, buttons.build_menu(2))

    except Exception as e:
        LOGGER.error(f"SeedrLink error: {e}")
        if torrent_id:
            try:
                await seedr_client.delete("torrent", torrent_id)
            except Exception:
                pass
        await _delete_seedr_folder(seedr_client, folder_id)
        await edit_message(
            msg,
            "<b>🛑 Seedr Link Generation Stopped</b>\n\n"
            f"<blockquote>• <b>Reason:</b> {escape(str(e))}\n"
            f"• <b>Time Elapsed:</b> {get_readable_time(time() - message.date.timestamp())}\n"
            "• <b>In Mode:</b> Seedr Cloud\n"
            f"• <b>User:</b> {tag}</blockquote>",
        )
