from ast import literal_eval
from asyncio import sleep
from base64 import b64encode
from html import escape
from os import path as ospath
from re import match as re_match, search as re_search
from time import time

from aiofiles import open as aiopen
from aiofiles.os import makedirs, listdir, path as aiopath
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
from ..helper.ext_utils.files_utils import clean_download
from ..helper.telegram_helper.message_utils import (
    auto_delete_message,
    delete_links,
    delete_message,
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
        msg_text = self.message.text or self.message.caption or ""
        text = msg_text.split("\n")
        input_list = text[0].split(" ") if text[0] else []

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

        if input_list and input_list[0].startswith("/"):
            arg_parser(input_list[1:], args)
        else:
            arg_parser(input_list, args)

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
        if args["-m"] or "-m" in self.options:
            self.manual_merge = True
            m_val = args["-m"]
            self.merge_custom_name = m_val if isinstance(m_val, str) else ""
        self.bot_trans = args["-bt"]
        self.user_trans = args["-ut"]
        self.is_alldebrid = args["-ad"]
        self.is_seedr = args["-seedr"] or self.is_seedr
        self.is_yt = args["-yt"]

        user_auto_merge = self.user_dict.get("AUTO_MERGE", False) or (
            "AUTO_MERGE" not in self.user_dict and getattr(Config, "AUTO_MERGE", False)
        )
        if user_auto_merge:
            self.auto_merge = True

        user_track_manager = self.user_dict.get("TRACK_MANAGER", False) or (
            "TRACK_MANAGER" not in self.user_dict and getattr(Config, "TRACK_MANAGER", False)
        )
        if user_track_manager:
            self.manual_reorder = True

        self.ht_flag = args["-ht"] or "-ht" in self.options

        from ..helper.ext_utils.task_manager import get_task_key
        task_source = self.link or (self.message.reply_to_message.text if self.message.reply_to_message and self.message.reply_to_message.text else "") or self.name
        self.task_key = get_task_key(task_source, self.user_id)

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
            is_subtask = getattr(self.message, "_is_bulk_subtask", False)
            if not is_subtask and getattr(self, "same_dir", None):
                for fd in self.same_dir.values():
                    if isinstance(fd, dict) and fd.get("total", 0) > 1:
                        is_subtask = len(fd.get("tasks", set())) > 1
                        if is_subtask:
                            break

            user_id = self.user_id
            event_done = bot_loop.create_future()
            ht_tasks[self.mid] = {
                "merge": self.auto_merge or self.manual_merge,
                "rm_stream": False,
                "reorder": False,
                "reorder_aud": [],
                "reorder_sub": [],
                "trim": False,
                "trim_range": "",
                "extract": False,
                "extract_types": [],
                "user_id": user_id,
                "future": event_done,
            }

            def build_ht_menu(mid):
                t_info = ht_tasks.get(mid, {})
                m_on = "✓ ON" if t_info.get("merge") else "OFF"
                tr_on = "✓ ON" if t_info.get("trim") else "OFF"
                ex_on = "✓ ON" if t_info.get("extract") else "OFF"
                tm_on = "✓ ON" if t_info.get("reorder") else "OFF"

                buttons = ButtonMaker()
                buttons.data_button(f"Merge: {m_on}", f"htmerge merge {mid}")
                buttons.data_button(f"Trim: {tr_on}", f"htmerge trim {mid}")
                buttons.data_button(f"Extract: {ex_on}", f"htmerge extract {mid}")
                buttons.data_button(f"Track Manager: {tm_on}", f"htmerge trackmgr {mid}")
                buttons.data_button("Done", f"htmerge done {mid}", position="footer")
                return buttons

            if is_subtask and hasattr(Mirror, "_last_ht_config") and Mirror._last_ht_config.get("user_id") == user_id:
                saved_ht = Mirror._last_ht_config.copy()
            else:
                prompt_msg = await send_message(
                    self.message,
                    f"<b>Task Received with -ht flag.</b>\nChoose pre-upload options:\n\n• <b>Merge:</b> OFF\n• <b>Trim:</b> OFF\n• <b>Extract:</b> OFF",
                    build_ht_menu(self.mid).build_menu(2),
                )
                try:
                    await event_done
                except Exception:
                    pass

                saved_ht = ht_tasks.get(self.mid, {})
                Mirror._last_ht_config = saved_ht.copy()
                await delete_message(prompt_msg)

            self.manual_merge = saved_ht.get("merge", False)
            self.manual_rm_stream = saved_ht.get("rm_stream", False)
            self.manual_reorder = saved_ht.get("reorder", False)
            self.reorder_aud = saved_ht.get("reorder_aud", [])
            self.reorder_sub = saved_ht.get("reorder_sub", [])
            self.aud_select = saved_ht.get("aud_select", None)
            self.sub_select = saved_ht.get("sub_select", None)
            self.aud_order = saved_ht.get("aud_order", None)
            self.sub_order = saved_ht.get("sub_order", None)
            self.manual_trim = saved_ht.get("trim", False)
            self.trim_range = saved_ht.get("trim_range", "")
            self.manual_extract = saved_ht.get("extract", False)
            self.extract_types = saved_ht.get("extract_types", [])

            ht_tasks.pop(self.mid, None)

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

        if not reply_to:
            reply_to = self.message

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
                    if not self.link:
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

        if self.is_leech and (not self.thumb or not await aiopath.exists(self.thumb)):
            from ..helper.ext_utils.media_utils import create_thumb
            if self.message.photo or (self.message.document and self.message.document.mime_type and self.message.document.mime_type.startswith("image/")):
                self.thumb = await create_thumb(self.message, self.user_id)
            elif self.message.reply_to_message and (
                self.message.reply_to_message.photo
                or (
                    self.message.reply_to_message.document
                    and self.message.reply_to_message.document.mime_type
                    and self.message.reply_to_message.document.mime_type.startswith("image/")
                )
            ):
                self.thumb = await create_thumb(self.message.reply_to_message, self.user_id)
            elif reply_to and (
                reply_to.photo
                or (
                    reply_to.document
                    and reply_to.document.mime_type
                    and reply_to.document.mime_type.startswith("image/")
                )
            ):
                self.thumb = await create_thumb(reply_to, self.user_id)

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


def sync_tm_to_task_info(mid):
    t_info = ht_tasks.get(mid, {})
    aud_tracks = t_info.get("tm_aud_tracks", [])
    sub_tracks = t_info.get("tm_sub_tracks", [])

    aud_order = [tr.get("orig_idx", i) for i, tr in enumerate(aud_tracks) if tr.get("selected", True)]
    sub_order = [tr.get("orig_idx", j) for j, tr in enumerate(sub_tracks) if tr.get("selected", True)]

    aud_select = [tr.get("orig_idx", i) for i, tr in enumerate(aud_tracks) if tr.get("selected", True)]
    sub_select = [tr.get("orig_idx", j) for j, tr in enumerate(sub_tracks) if tr.get("selected", True)]

    init_aud = t_info.get("initial_aud_count", len(aud_tracks))
    init_sub = t_info.get("initial_sub_count", len(sub_tracks))

    if len(aud_order) != init_aud or aud_order != list(range(init_aud)):
        t_info["aud_order"] = aud_order
        t_info["aud_select"] = aud_select
    else:
        t_info["aud_order"] = None
        t_info["aud_select"] = None

    if len(sub_order) != init_sub or sub_order != list(range(init_sub)):
        t_info["sub_order"] = sub_order
        t_info["sub_select"] = sub_select
    else:
        t_info["sub_order"] = None
        t_info["sub_select"] = None

    t_info["reorder"] = True


@new_task
async def ht_merge_callback(client, query):
    data = query.data.split()
    mid = int(data[2])
    task_info = ht_tasks.get(mid)
    if not task_info:
        return await query.answer("Task expired or already started!", show_alert=True)
    if query.from_user.id != task_info["user_id"]:
        return await query.answer("This menu is not for you!", show_alert=True)

    def render_ht_menu(mid):
        t_info = ht_tasks.get(mid, {})
        buttons = ButtonMaker()
        buttons.data_button(f"Merge: {'✓ ON' if t_info.get('merge') else 'OFF'}", f"htmerge merge {mid}")
        buttons.data_button(f"Trim: {'✓ ON' if t_info.get('trim') else 'OFF'}", f"htmerge trim {mid}")
        buttons.data_button(f"Extract: {'✓ ON' if t_info.get('extract') else 'OFF'}", f"htmerge extract {mid}")
        buttons.data_button(f"Track Manager: {'✓ ON' if t_info.get('reorder') else 'OFF'}", f"htmerge trackmgr {mid}")
        buttons.data_button("Done", f"htmerge done {mid}", position="footer")
        return buttons

    def render_ht_text(mid):
        t_info = ht_tasks.get(mid, {})
        return (
            f"<b>Task Received with -ht flag.</b>\nChoose pre-upload options:\n\n"
            f"• <b>Merge:</b> {'✓ ON' if t_info.get('merge') else 'OFF'}\n"
            f"• <b>Trim:</b> {'✓ ON' if t_info.get('trim') else 'OFF'} ({t_info.get('trim_range') or 'Not Set'})\n"
            f"• <b>Extract:</b> {'✓ ON' if t_info.get('extract') else 'OFF'} ({', '.join(t_info.get('extract_types', [])) or 'Not Set'})\n"
            f"• <b>Track Manager:</b> {'✓ ON' if t_info.get('reorder') else 'OFF'}"
        )

    def render_trackmgr_menu(mid):
        t_info = ht_tasks.get(mid, {})
        aud_tracks = t_info.get("tm_aud_tracks")
        sub_tracks = t_info.get("tm_sub_tracks")

        if aud_tracks is None or sub_tracks is None:
            from os import walk
            import json
            from subprocess import run as srun, PIPE

            task_dir = f"{DOWNLOAD_DIR}{mid}"
            found_media = None
            if ospath.exists(task_dir):
                for root, _, files in walk(task_dir):
                    for file_ in files:
                        fp = ospath.join(root, file_)
                        ext = ospath.splitext(fp)[1].lower()
                        if ext in (".mkv", ".mp4", ".webm", ".avi", ".mov", ".flv", ".m4v", ".ts", ".3gp"):
                            found_media = fp
                            break
                    if found_media:
                        break

            if found_media:
                try:
                    res = srun(["ffprobe", "-hide_banner", "-loglevel", "error", "-print_format", "json", "-show_streams", found_media], stdout=PIPE, stderr=PIPE)
                    if res.returncode == 0:
                        raw_streams = json.loads(res.stdout).get("streams", [])
                        prob_aud = []
                        prob_sub = []
                        a_idx, s_idx = 0, 0
                        for st in raw_streams:
                            st_type = st.get("codec_type")
                            idx = st.get("index", 0)
                            lang = st.get("tags", {}).get("language") or st.get("tags", {}).get("title") or "und"
                            codec = st.get("codec_name", "audio")
                            if st_type == "audio":
                                prob_aud.append({"idx": a_idx, "orig_idx": a_idx, "global_idx": idx, "lang": lang, "codec": codec, "selected": True})
                                a_idx += 1
                            elif st_type == "subtitle":
                                prob_sub.append({"idx": s_idx, "orig_idx": s_idx, "global_idx": idx, "lang": lang, "codec": codec, "selected": True})
                                s_idx += 1
                        if prob_aud or prob_sub:
                            aud_tracks = prob_aud
                            sub_tracks = prob_sub
                except Exception:
                    pass

            if aud_tracks is None:
                aud_tracks = []
            if sub_tracks is None:
                sub_tracks = []

            t_info["tm_aud_tracks"] = aud_tracks
            t_info["tm_sub_tracks"] = sub_tracks
            t_info["initial_aud_count"] = len(aud_tracks)
            t_info["initial_sub_count"] = len(sub_tracks)

        buttons = ButtonMaker()
        buttons.data_button("Select All", f"htmerge tm_select_all {mid}")
        buttons.data_button("Remove Unselected", f"htmerge tm_rem_unselected {mid}")
        buttons.data_button("Keep All", f"htmerge tm_keep_all {mid}")

        buttons.data_button("--- AUDIO TRACKS ---", f"htmerge dummy {mid}", position="header")
        for i, tr in enumerate(aud_tracks):
            st = "✓" if tr.get("selected", True) else "x"
            lang = tr.get("lang", "und")
            codec = tr.get("codec", "audio")
            buttons.data_button(f"[{st}] Aud {i+1}: {lang} ({codec})", f"htmerge tm_toggle_aud_{i} {mid}")
            buttons.data_button("🔼", f"htmerge tm_up_aud_{i} {mid}")
            buttons.data_button("🔽", f"htmerge tm_down_aud_{i} {mid}")

        buttons.data_button("--- SUBTITLE TRACKS ---", f"htmerge dummy {mid}", position="header")
        for j, tr in enumerate(sub_tracks):
            st = "✓" if tr.get("selected", True) else "x"
            lang = tr.get("lang", "und")
            codec = tr.get("codec", "sub")
            buttons.data_button(f"[{st}] Sub {j+1}: {lang} ({codec})", f"htmerge tm_toggle_sub_{j} {mid}")
            buttons.data_button("🔼", f"htmerge tm_up_sub_{j} {mid}")
            buttons.data_button("🔽", f"htmerge tm_down_sub_{j} {mid}")

        buttons.data_button("Done", f"htmerge tm_done {mid}", position="footer")
        return buttons

    if data[1] in ["merge"]:
        key = data[1]
        task_info[key] = not task_info[key]
        await query.answer(f"{key.replace('_', ' ').title()} turned {'ON' if task_info[key] else 'OFF'}")
        await edit_message(query.message, render_ht_text(mid), render_ht_menu(mid).build_menu(2))
    elif data[1] == "trim":
        await query.answer()
        buttons = ButtonMaker()
        buttons.data_button("◀️ Back", f"htmerge back {mid}", position="footer")
        prompt = "<b>✂️ Trim Media:</b>\nPlease send trim range in format: <code>HH:MM:SS - HH:MM:SS</code>\nExample: <code>00:20:07 - 00:30:08</code>\n⏱️ <i>Timeout: 30s</i>"
        await edit_message(query.message, prompt, buttons.build_menu(1))

        event_done = bot_loop.create_future()
        user_input = []

        async def trim_filter(_, __, event):
            u = event.from_user or event.sender_chat
            return bool(u and u.id == task_info["user_id"] and event.chat.id == query.message.chat.id and event.text)

        async def trim_handler(_, msg):
            user_input.append(msg.text.strip())
            await delete_message(msg)
            if not event_done.done():
                event_done.set_result(True)

        from pyrogram.handlers import MessageHandler
        from pyrogram.filters import create
        from asyncio import wait_for
        h = client.add_handler(MessageHandler(trim_handler, filters=create(trim_filter)), group=-1)
        try:
            await wait_for(event_done, timeout=30)
            if user_input and "-" in user_input[0]:
                task_info["trim"] = True
                task_info["trim_range"] = user_input[0]
        except Exception:
            pass
        finally:
            client.remove_handler(*h)
            await edit_message(query.message, render_ht_text(mid), render_ht_menu(mid).build_menu(2))
    elif data[1] == "chorder":
        await query.answer()
        buttons = ButtonMaker()
        buttons.data_button("◀️ Back", f"htmerge back {mid}", position="footer")
        prompt = (
            "<b>🔀 Send new track order format:</b>\n"
            "• <code>aud=1:2</code> — Swap audio track 1 with track 2\n"
            "• <code>sub=1:2</code> — Swap subtitle track 1 with track 2\n"
            "• Multiple: <code>aud=1:2,3:4, sub=1:2</code>\n⏱️ <i>Timeout: 30s</i>"
        )
        await edit_message(query.message, prompt, buttons.build_menu(1))

        event_done = bot_loop.create_future()
        user_input = []

        async def reorder_filter(_, __, event):
            u = event.from_user or event.sender_chat
            return bool(u and u.id == task_info["user_id"] and event.chat.id == query.message.chat.id and event.text)

        async def reorder_handler(_, msg):
            user_input.append(msg.text.strip())
            await delete_message(msg)
            if not event_done.done():
                event_done.set_result(True)

        from pyrogram.handlers import MessageHandler
        from pyrogram.filters import create
        from asyncio import wait_for
        h = client.add_handler(MessageHandler(reorder_handler, filters=create(reorder_filter)), group=-1)
        try:
            await wait_for(event_done, timeout=30)
            if user_input:
                inp = user_input[0]
                aud_swaps, sub_swaps = [], []
                for item in inp.split(","):
                    item = item.strip()
                    if item.startswith("aud="):
                        val = item.split("=", 1)[1]
                        nums = [int(x) for x in val.split(":") if x.isdigit()]
                        for k in range(0, len(nums) - 1, 2):
                            aud_swaps.append([nums[k], nums[k+1]])
                    elif item.startswith("sub="):
                        val = item.split("=", 1)[1]
                        nums = [int(x) for x in val.split(":") if x.isdigit()]
                        for k in range(0, len(nums) - 1, 2):
                            sub_swaps.append([nums[k], nums[k+1]])
                task_info["reorder"] = True
                task_info["reorder_aud"] = aud_swaps
                task_info["reorder_sub"] = sub_swaps
        except Exception:
            pass
        finally:
            client.remove_handler(*h)
            await edit_message(query.message, render_ht_text(mid), render_ht_menu(mid).build_menu(2))
    elif data[1] == "extract":
        await query.answer()
        selected_ext = task_info.get("extract_types", [])

        if len(data) > 3:
            ex_type = data[3]
            if ex_type == "all":
                selected_ext = ["video", "audio", "subtitle"]
            elif ex_type in selected_ext:
                selected_ext.remove(ex_type)
            else:
                selected_ext.append(ex_type)
            task_info["extract_types"] = selected_ext
            task_info["extract"] = bool(selected_ext)

        buttons = ButtonMaker()
        v_st = "✓ " if "video" in selected_ext else ""
        a_st = "✓ " if "audio" in selected_ext else ""
        s_st = "✓ " if "subtitle" in selected_ext else ""

        buttons.data_button(f"{v_st}Video/File", f"htmerge extract {mid} video")
        buttons.data_button(f"{a_st}Audio", f"htmerge extract {mid} audio")
        buttons.data_button(f"{s_st}Subtitle", f"htmerge extract {mid} subtitle")
        buttons.data_button("All", f"htmerge extract {mid} all")
        buttons.data_button("Done", f"htmerge back {mid}", position="footer")

        caption = f"<b>📦 Select Extraction Types:</b>\nActive: {', '.join(selected_ext) or 'None'}"
        await edit_message(query.message, caption, buttons.build_menu(2))
    elif data[1] == "dummy":
        await query.answer()
    elif data[1] == "trackmgr":
        task_info["reorder"] = not task_info.get("reorder", False)
        await query.answer(f"Track Manager turned {'ON' if task_info['reorder'] else 'OFF'}")
        await edit_message(query.message, render_ht_text(mid), render_ht_menu(mid).build_menu(2))
    elif data[1] == "tm_select_all":
        await query.answer("Selected all tracks")
        for tr in task_info.get("tm_aud_tracks", []):
            tr["selected"] = True
        for tr in task_info.get("tm_sub_tracks", []):
            tr["selected"] = True
        sync_tm_to_task_info(mid)
        await edit_message(query.message, "<b>🎵 Track Manager:</b>\nSelect, reorder, or filter audio and subtitle tracks:", render_trackmgr_menu(mid).build_menu(3))
    elif data[1] == "tm_rem_unselected":
        await query.answer("Removed unselected tracks")
        task_info["tm_aud_tracks"] = [tr for tr in task_info.get("tm_aud_tracks", []) if tr.get("selected", True)]
        task_info["tm_sub_tracks"] = [tr for tr in task_info.get("tm_sub_tracks", []) if tr.get("selected", True)]
        sync_tm_to_task_info(mid)
        await edit_message(query.message, "<b>🎵 Track Manager:</b>\nSelect, reorder, or filter audio and subtitle tracks:", render_trackmgr_menu(mid).build_menu(3))
    elif data[1] == "tm_keep_all":
        await query.answer("Keeping all tracks")
        for tr in task_info.get("tm_aud_tracks", []):
            tr["selected"] = True
        for tr in task_info.get("tm_sub_tracks", []):
            tr["selected"] = True
        sync_tm_to_task_info(mid)
        await edit_message(query.message, "<b>🎵 Track Manager:</b>\nSelect, reorder, or filter audio and subtitle tracks:", render_trackmgr_menu(mid).build_menu(3))
    elif data[1].startswith("tm_toggle_aud_"):
        await query.answer()
        idx = int(data[1].replace("tm_toggle_aud_", ""))
        tracks = task_info.get("tm_aud_tracks", [])
        if 0 <= idx < len(tracks):
            tracks[idx]["selected"] = not tracks[idx].get("selected", True)
        sync_tm_to_task_info(mid)
        await edit_message(query.message, "<b>🎵 Track Manager:</b>\nSelect, reorder, or filter audio and subtitle tracks:", render_trackmgr_menu(mid).build_menu(3))
    elif data[1].startswith("tm_toggle_sub_"):
        await query.answer()
        idx = int(data[1].replace("tm_toggle_sub_", ""))
        tracks = task_info.get("tm_sub_tracks", [])
        if 0 <= idx < len(tracks):
            tracks[idx]["selected"] = not tracks[idx].get("selected", True)
        sync_tm_to_task_info(mid)
        await edit_message(query.message, "<b>🎵 Track Manager:</b>\nSelect, reorder, or filter audio and subtitle tracks:", render_trackmgr_menu(mid).build_menu(3))
    elif data[1].startswith("tm_up_aud_"):
        await query.answer()
        idx = int(data[1].replace("tm_up_aud_", ""))
        tracks = task_info.get("tm_aud_tracks", [])
        if idx > 0 and idx < len(tracks):
            tracks[idx], tracks[idx-1] = tracks[idx-1], tracks[idx]
        sync_tm_to_task_info(mid)
        await edit_message(query.message, "<b>🎵 Track Manager:</b>\nSelect, reorder, or filter audio and subtitle tracks:", render_trackmgr_menu(mid).build_menu(3))
    elif data[1].startswith("tm_down_aud_"):
        await query.answer()
        idx = int(data[1].replace("tm_down_aud_", ""))
        tracks = task_info.get("tm_aud_tracks", [])
        if idx >= 0 and idx < len(tracks) - 1:
            tracks[idx], tracks[idx+1] = tracks[idx+1], tracks[idx]
        sync_tm_to_task_info(mid)
        await edit_message(query.message, "<b>🎵 Track Manager:</b>\nSelect, reorder, or filter audio and subtitle tracks:", render_trackmgr_menu(mid).build_menu(3))
    elif data[1].startswith("tm_up_sub_"):
        await query.answer()
        idx = int(data[1].replace("tm_up_sub_", ""))
        tracks = task_info.get("tm_sub_tracks", [])
        if idx > 0 and idx < len(tracks):
            tracks[idx], tracks[idx-1] = tracks[idx-1], tracks[idx]
        sync_tm_to_task_info(mid)
        await edit_message(query.message, "<b>🎵 Track Manager:</b>\nSelect, reorder, or filter audio and subtitle tracks:", render_trackmgr_menu(mid).build_menu(3))
    elif data[1].startswith("tm_down_sub_"):
        await query.answer()
        idx = int(data[1].replace("tm_down_sub_", ""))
        tracks = task_info.get("tm_sub_tracks", [])
        if idx >= 0 and idx < len(tracks) - 1:
            tracks[idx], tracks[idx+1] = tracks[idx+1], tracks[idx]
        sync_tm_to_task_info(mid)
        await edit_message(query.message, "<b>🎵 Track Manager:</b>\nSelect, reorder, or filter audio and subtitle tracks:", render_trackmgr_menu(mid).build_menu(3))
    elif data[1] == "back":
        await query.answer()
        await edit_message(query.message, render_ht_text(mid), render_ht_menu(mid).build_menu(2))
    elif data[1] in ["done", "tm_done"]:
        await query.answer("Starting task...")
        sync_tm_to_task_info(mid)
        fut = task_info.get("future")
        if fut and not fut.done():
            fut.set_result(True)


async def prompt_track_manager(listener, media_file):
    from asyncio import wait_for, get_running_loop
    from ..helper.ext_utils.media_utils import FFMpeg
    from ..helper.mirror_leech_utils.status_utils.trackmgr_status import TrackManagerStatus

    ffmpeg = FFMpeg(listener)
    streams = await ffmpeg.get_streams(media_file)
    if not streams:
        return

    aud_tracks = []
    sub_tracks = []
    a_idx, s_idx = 0, 0
    for st in streams:
        st_type = st.get("codec_type")
        idx = st.get("index", 0)
        lang = st.get("tags", {}).get("language") or st.get("tags", {}).get("title") or "und"
        codec = st.get("codec_name", "audio")
        if st_type == "audio":
            aud_tracks.append({"idx": a_idx, "orig_idx": a_idx, "global_idx": idx, "lang": lang, "codec": codec, "selected": True})
            a_idx += 1
        elif st_type == "subtitle":
            sub_tracks.append({"idx": s_idx, "orig_idx": s_idx, "global_idx": idx, "lang": lang, "codec": codec, "selected": True})
            s_idx += 1

    if not aud_tracks and not sub_tracks:
        return

    mid = listener.mid
    try:
        loop = get_running_loop()
        event_done = loop.create_future()
    except Exception:
        event_done = bot_loop.create_future()

    ht_tasks[mid] = {
        "user_id": listener.user_id,
        "tm_aud_tracks": aud_tracks,
        "tm_sub_tracks": sub_tracks,
        "initial_aud_count": len(aud_tracks),
        "initial_sub_count": len(sub_tracks),
        "future": event_done,
        "reorder": True,
    }

    gid = f"tm_{mid}"
    async with task_dict_lock:
        task_dict[mid] = TrackManagerStatus(listener, gid)

    def render_trackmgr_menu(mid):
        t_info = ht_tasks.get(mid, {})

        buttons = ButtonMaker()
        buttons.data_button("Select All", f"htmerge tm_select_all {mid}")
        buttons.data_button("Remove Unselected", f"htmerge tm_rem_unselected {mid}")
        buttons.data_button("Keep All", f"htmerge tm_keep_all {mid}")

        if not aud_tracks and not sub_tracks:
            buttons.data_button("No Audio/Subtitle Tracks Found", f"htmerge dummy {mid}", position="header")
        else:
            if aud_tracks:
                buttons.data_button("--- AUDIO TRACKS ---", f"htmerge dummy {mid}", position="header")
                for i, tr in enumerate(aud_tracks):
                    st = "✓" if tr.get("selected", True) else "x"
                    lang = tr.get("lang", "und")
                    codec = tr.get("codec", "audio")
                    buttons.data_button(f"[{st}] Aud {i+1}: {lang} ({codec})", f"htmerge tm_toggle_aud_{i} {mid}")
                    buttons.data_button("🔼", f"htmerge tm_up_aud_{i} {mid}")
                    buttons.data_button("🔽", f"htmerge tm_down_aud_{i} {mid}")

            if sub_tracks:
                buttons.data_button("--- SUBTITLE TRACKS ---", f"htmerge dummy {mid}", position="header")
                for j, tr in enumerate(sub_tracks):
                    st = "✓" if tr.get("selected", True) else "x"
                    lang = tr.get("lang", "und")
                    codec = tr.get("codec", "sub")
                    buttons.data_button(f"[{st}] Sub {j+1}: {lang} ({codec})", f"htmerge tm_toggle_sub_{j} {mid}")
                    buttons.data_button("🔼", f"htmerge tm_up_sub_{j} {mid}")
                    buttons.data_button("🔽", f"htmerge tm_down_sub_{j} {mid}")

        buttons.data_button("Done", f"htmerge tm_done {mid}", position="footer")
        return buttons

    prompt_msg = None
    try:
        prompt_msg = await send_message(
            listener.user_id,
            f"<b>🎵 Track Manager ({ospath.basename(media_file)}):</b>\nSelect, reorder, or filter audio and subtitle tracks before upload:",
            render_trackmgr_menu(mid).build_menu(3),
        )
    except Exception as err:
        LOGGER.warning(f"Failed to send Track Manager menu to DM ({err}), falling back to chat message.")
        try:
            prompt_msg = await send_message(
                listener.message,
                f"<b>🎵 Track Manager ({ospath.basename(media_file)}):</b>\nSelect, reorder, or filter audio and subtitle tracks before upload:",
                render_trackmgr_menu(mid).build_menu(3),
            )
        except Exception as e:
            LOGGER.error(f"Failed to send Track Manager menu: {e}")

    try:
        await wait_for(event_done, timeout=180)
    except Exception:
        pass

    sync_tm_to_task_info(mid)
    saved_ht = ht_tasks.get(mid, {})
    listener.aud_select = saved_ht.get("aud_select", None)
    listener.sub_select = saved_ht.get("sub_select", None)
    listener.aud_order = saved_ht.get("aud_order", None)
    listener.sub_order = saved_ht.get("sub_order", None)

    ht_tasks.pop(mid, None)
    if prompt_msg:
        await delete_message(prompt_msg)


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


@new_task
async def auto_task_handler(client, message):
    from .users_settings import handler_dict

    user_id = message.from_user.id if message.from_user else (message.sender_chat.id if message.sender_chat else 0)
    if not user_id or handler_dict.get(user_id, False):
        return

    msg_text = message.text or message.caption or ""
    if msg_text.strip().startswith("/"):
        return

    user_dict = user_data.get(user_id, {})
    auto_leech = user_dict.get("AUTO_LEECH", False)
    auto_mirror = user_dict.get("AUTO_MIRROR", False)
    auto_ddl = user_dict.get("AUTO_DDL", False)

    if not (auto_leech or auto_mirror or auto_ddl):
        return

    file_ = (
        message.document
        or message.photo
        or message.video
        or message.audio
        or message.voice
        or message.video_note
        or message.sticker
        or message.animation
        or None
    )

    reply_to = message.reply_to_message
    if not file_ and reply_to:
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

    has_link = bool(
        is_url(msg_text)
        or is_magnet(msg_text)
        or is_telegram_link(msg_text)
        or is_rclone_path(msg_text)
        or is_gdrive_link(msg_text)
        or is_gdrive_id(msg_text)
        or is_mega_link(msg_text)
        or ("http://" in msg_text or "https://" in msg_text or "magnet:" in msg_text)
    )

    if not file_ and not has_link and reply_to and reply_to.text:
        rtext = reply_to.text
        has_link = bool(
            is_url(rtext)
            or is_magnet(rtext)
            or is_telegram_link(rtext)
            or is_rclone_path(rtext)
            or is_gdrive_link(rtext)
            or is_gdrive_id(rtext)
            or is_mega_link(rtext)
            or ("http://" in rtext or "https://" in rtext or "magnet:" in rtext)
        )

    if not file_ and not has_link:
        return

    if auto_leech:
        await leech(client, message)
    if auto_mirror:
        await mirror(client, message)
    if auto_ddl:
        await uphoster(client, message)


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
