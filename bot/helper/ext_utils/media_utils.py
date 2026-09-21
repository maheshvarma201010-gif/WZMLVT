import re
from ast import literal_eval
from contextlib import suppress
from PIL import Image
from hashlib import md5, sha256
from aiofiles import open as aiopen
from aiofiles.os import remove, path as aiopath, makedirs
import json
from asyncio import (
    create_subprocess_exec,
    gather,
    wait_for,
    sleep,
)
from asyncio.subprocess import PIPE
from os import path as ospath
from re import search as re_search, escape
from time import time
from aioshutil import move, rmtree
from langcodes import Language
from niquests import AsyncSession

from ... import LOGGER, DOWNLOAD_DIR
from ...core.cpu import ffmpeg_layout
from ...core.config_manager import BinConfig
from .bot_utils import cmd_exec, sync_to_async
from .files_utils import get_mime_type, is_archive, is_archive_split
from .status_utils import time_to_seconds


def get_md5_hash(up_path):
    md5_hash = md5()
    with open(up_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            md5_hash.update(byte_block)
        return md5_hash.hexdigest()


def _convert_image(src, dst):
    with Image.open(src) as im:
        im.convert("RGB").save(dst, "JPEG", quality=95)


async def create_thumb(msg, _id=""):
    if not _id:
        _id = int(time() * 1000)
        path = f"{DOWNLOAD_DIR}thumbnails"
    else:
        path = "thumbnails"
    await makedirs(path, exist_ok=True)
    try:
        photo_dir = await msg.download()
    except Exception as e:
        LOGGER.error(f"Failed to download photo: {e}")
        return ""
    output = ospath.join(path, f"{_id}.jpg")
    try:
        await sync_to_async(_convert_image, photo_dir, output)
    except Exception as e:
        LOGGER.error(f"Failed to process thumb: {e}")
        await remove(photo_dir)
        return ""
    await remove(photo_dir)
    return output


async def download_image_thumb(url):
    NON_IMAGE_TYPES = (
        "text/",
        "application/json",
        "application/xml",
        "application/javascript",
        "video/",
        "audio/",
    )
    path = f"{DOWNLOAD_DIR}thumbnails"
    await makedirs(path, exist_ok=True)

    try:
        async with AsyncSession() as client:
            try:
                head_resp = await client.head(url, allow_redirects=True)
                ct = head_resp.headers.get("content-type", "")
                if ct and any(ct.startswith(t) for t in NON_IMAGE_TYPES):
                    LOGGER.error(f"Thumb URL is not an image: {ct}")
                    return ""
            except Exception:
                pass

            resp = await client.get(url, allow_redirects=True, timeout=30)
            if resp.status_code != 200:
                LOGGER.error(f"Failed to download thumb URL: HTTP {resp.status_code}")
                return ""

            data = resp.content
    except Exception as e:
        LOGGER.error(f"Error downloading thumb from URL: {e}")
        return ""

    tag = sha256(url.encode()).hexdigest()[:12]
    tmp_path = ospath.join(path, f"{tag}_tmp")
    output = ospath.join(path, f"{tag}.jpg")

    try:
        async with aiopen(tmp_path, "wb") as f:
            await f.write(data)
    except Exception as e:
        LOGGER.error(f"Failed to write thumb temp file: {e}")
        return ""

    try:
        await sync_to_async(_convert_image, tmp_path, output)
    except Exception as e:
        LOGGER.error(f"Failed to process thumb image: {e}")
        with suppress(Exception):
            await remove(tmp_path)
        return ""
    with suppress(Exception):
        await remove(tmp_path)
    return output


async def get_media_info(path, extra_info=False):
    try:
        result = await cmd_exec(
            [
                "ffprobe",
                "-hide_banner",
                "-loglevel",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                path,
            ]
        )
    except Exception as e:
        LOGGER.error(f"Get Media Info: {e}. Mostly File not found! - File: {path}")
        return (0, "", "", "") if extra_info else (0, None, None)
    if result[0] and result[2] == 0:
        ffresult = literal_eval(result[0])
        if not isinstance(ffresult, dict):
            LOGGER.error(f"get_media_info: unexpected ffprobe payload: {result}")
            return (0, "", "", "") if extra_info else (0, None, None)
        fields = ffresult.get("format")
        if fields is None:
            LOGGER.error(f"get_media_info: {result}")
            return (0, "", "", "") if extra_info else (0, None, None)
        duration = round(float(fields.get("duration", 0)))
        if extra_info:
            lang, qual, stitles = "", "", ""
            if (streams := ffresult.get("streams")) and streams[0].get(
                "codec_type"
            ) == "video":
                qual = int(streams[0].get("height"))
                qual = f"{480 if qual <= 480 else 540 if qual <= 540 else 720 if qual <= 720 else 1080 if qual <= 1080 else 2160 if qual <= 2160 else 4320 if qual <= 4320 else 8640}p"
                for stream in streams:
                    if stream.get("codec_type") == "audio" and (
                        lc := stream.get("tags", {}).get("language")
                    ):
                        with suppress(Exception):
                            lc = Language.get(lc).display_name()
                        if lc not in lang:
                            lang += f"{lc}, "
                    if stream.get("codec_type") == "subtitle" and (
                        st := stream.get("tags", {}).get("language")
                    ):
                        with suppress(Exception):
                            st = Language.get(st).display_name()
                        if st not in stitles:
                            stitles += f"{st}, "
            return duration, qual, lang[:-2], stitles[:-2]
        tags = fields.get("tags", {})
        artist = tags.get("artist") or tags.get("ARTIST") or tags.get("Artist")
        title = tags.get("title") or tags.get("TITLE") or tags.get("Title")
        return duration, artist, title
    return (0, "", "", "") if extra_info else (0, None, None)


async def get_document_type(path):
    is_video, is_audio, is_image = False, False, False
    if (
        is_archive(path)
        or is_archive_split(path)
        or re_search(r".+(\.|_)(rar|7z|zip|bin)(\.0*\d+)?$", path)
    ):
        return is_video, is_audio, is_image
    mime_type = await sync_to_async(get_mime_type, path)
    if mime_type.startswith("image"):
        return False, False, True
    if mime_type.startswith("text"):
        return False, False, False
    try:
        result = await cmd_exec(
            [
                "ffprobe",
                "-hide_banner",
                "-loglevel",
                "error",
                "-print_format",
                "json",
                "-show_streams",
                path,
            ]
        )
        if result[1] and mime_type.startswith("video"):
            is_video = True
    except Exception as e:
        LOGGER.error(f"Get Document Type: {e}. Mostly File not found! - File: {path}")
        if mime_type.startswith("audio"):
            return False, True, False
        if not mime_type.startswith("video") and not mime_type.endswith("octet-stream"):
            return is_video, is_audio, is_image
        if mime_type.startswith("video"):
            is_video = True
        return is_video, is_audio, is_image
    if result[0] and result[2] == 0:
        fields = literal_eval(result[0]).get("streams")
        if fields is None:
            LOGGER.error(f"get_document_type: {result}")
            return is_video, is_audio, is_image
        is_video = False
        for stream in fields:
            if stream.get("codec_type") == "video":
                codec_name = stream.get("codec_name", "").lower()
                if codec_name not in {"mjpeg", "png", "bmp"}:
                    is_video = True
            elif stream.get("codec_type") == "audio":
                is_audio = True
    return is_video, is_audio, is_image


async def get_streams(file):
    cmd = [
        "ffprobe",
        "-hide_banner",
        "-loglevel",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        file,
    ]
    process = await create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        LOGGER.error(f"Error getting stream info: {stderr.decode().strip()}")
        return None

    try:
        return json.loads(stdout)["streams"]
    except KeyError:
        LOGGER.error(
            f"No streams found in the ffprobe output: {stdout.decode().strip()}",
        )
        return None


async def take_ss(video_file, ss_nb) -> bool:
    cores, threads = ffmpeg_layout()
    duration = (await get_media_info(video_file))[0]
    if duration != 0:
        dirpath, name = video_file.rsplit("/", 1)
        name, _ = ospath.splitext(name)
        dirpath = f"{dirpath}/{name}_mltbss"
        await makedirs(dirpath, exist_ok=True)
        interval = duration // (ss_nb + 1)
        cap_time = interval
        cmds = []
        for i in range(ss_nb):
            output = f"{dirpath}/SS.{name}_{i:02}.png"
            cmd = [
                "taskset",
                "-c",
                f"{cores}",
                BinConfig.FFMPEG_NAME,
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{cap_time}",
                "-i",
                video_file,
                "-q:v",
                "1",
                "-frames:v",
                "1",
                "-threads",
                f"{threads}",
                output,
            ]
            cap_time += interval
            cmds.append(cmd_exec(cmd))
        try:
            resutls = await wait_for(gather(*cmds), timeout=60)
            if resutls[0][2] != 0:
                LOGGER.error(
                    f"Error while creating screenshots from video. Path: {video_file}. stderr: {resutls[0][1]}"
                )
                await rmtree(dirpath, ignore_errors=True)
                return False
        except Exception:
            LOGGER.error(
                f"Error while creating screenshots from video. Path: {video_file}. Error: Timeout some issues with ffmpeg with specific arch!"
            )
            await rmtree(dirpath, ignore_errors=True)
            return False
        return dirpath
    else:
        LOGGER.error("take_ss: Can't get the duration of video")
        return False


async def get_audio_thumbnail(audio_file):
    cores, threads = ffmpeg_layout()
    output_dir = f"{DOWNLOAD_DIR}thumbnails"
    await makedirs(output_dir, exist_ok=True)
    output = ospath.join(output_dir, f"{time()}.jpg")
    cmd = [
        "taskset",
        "-c",
        f"{cores}",
        BinConfig.FFMPEG_NAME,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        audio_file,
        "-an",
        "-vcodec",
        "copy",
        "-threads",
        f"{threads}",
        output,
    ]
    try:
        _, err, code = await wait_for(cmd_exec(cmd), timeout=60)
        if code != 0 or not await aiopath.exists(output):
            LOGGER.warning(
                f"Could not extract thumbnail from audio. Name: {audio_file} stderr: {err}"
            )
            return None
    except Exception:
        LOGGER.warning(
            f"Could not extract thumbnail from audio. Name: {audio_file}. Timeout or ffmpeg issue."
        )
        return None
    return output


async def get_video_thumbnail(video_file, duration):
    cores, threads = ffmpeg_layout()
    output_dir = f"{DOWNLOAD_DIR}thumbnails"
    await makedirs(output_dir, exist_ok=True)
    output = ospath.join(output_dir, f"{time()}.jpg")
    if duration is None:
        duration = (await get_media_info(video_file))[0]
    if duration == 0:
        duration = 3
    duration = duration // 2
    cmd = [
        "taskset",
        "-c",
        f"{cores}",
        BinConfig.FFMPEG_NAME,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{duration}",
        "-i",
        video_file,
        "-vf",
        "thumbnail,format=yuv420p",
        "-q:v",
        "1",
        "-frames:v",
        "1",
        "-threads",
        f"{threads}",
        output,
    ]
    try:
        _, err, code = await wait_for(cmd_exec(cmd), timeout=60)
        if code != 0 or not await aiopath.exists(output):
            LOGGER.error(
                f"Error while extracting thumbnail from video. Name: {video_file} stderr: {err}"
            )
            return None
    except Exception:
        LOGGER.error(
            f"Error while extracting thumbnail from video. Name: {video_file}. Error: Timeout some issues with ffmpeg with specific arch!"
        )
        return None
    return output


async def get_multiple_frames_thumbnail(video_file, layout, keep_screenshots):
    cores, threads = ffmpeg_layout()
    layout = re.sub(r"(\d+)\D+(\d+)", r"\1x\2", layout)
    ss_nb = layout.split("x")
    if len(ss_nb) != 2 or not ss_nb[0].isdigit() or not ss_nb[1].isdigit():
        LOGGER.error(f"Invalid layout value: {layout}")
        return None
    ss_nb = int(ss_nb[0]) * int(ss_nb[1])
    if ss_nb == 0:
        LOGGER.error(f"Invalid layout value: {layout}")
        return None
    dirpath = await take_ss(video_file, ss_nb)
    if not dirpath:
        return None
    output_dir = f"{DOWNLOAD_DIR}thumbnails"
    await makedirs(output_dir, exist_ok=True)
    output = ospath.join(output_dir, f"{time()}.jpg")
    cmd = [
        "taskset",
        "-c",
        f"{cores}",
        BinConfig.FFMPEG_NAME,
        "-hide_banner",
        "-loglevel",
        "error",
        "-pattern_type",
        "glob",
        "-i",
        f"{escape(dirpath)}/*.png",
        "-vf",
        f"tile={layout},thumbnail,format=yuv420p",
        "-q:v",
        "1",
        "-frames:v",
        "1",
        "-f",
        "mjpeg",
        "-threads",
        f"{threads}",
        output,
    ]
    try:
        _, err, code = await wait_for(cmd_exec(cmd), timeout=60)
        if code != 0 or not await aiopath.exists(output):
            LOGGER.error(
                f"Error while combining thumbnails for video. Name: {video_file} stderr: {err}"
            )
            return None
    except Exception:
        LOGGER.error(
            f"Error while combining thumbnails from video. Name: {video_file}. Error: Timeout some issues with ffmpeg with specific arch!"
        )
        return None
    finally:
        if not keep_screenshots:
            await rmtree(dirpath, ignore_errors=True)
    return output


def parse_track_spec(input_str):
    res = {"audio": [], "subtitle": []}
    if not input_str or not isinstance(input_str, str):
        return res
    lines = [p.strip() for p in re.split(r"[|;\n]", input_str) if p.strip()]
    for line in lines:
        if "=" in line:
            k, v = line.split("=", 1)
            k = k.strip().lower()
            items = [x.strip() for x in v.split(",") if x.strip()]
            if k in ["audio", "aud"] or k.startswith("audio") or k.startswith("aud"):
                res["audio"].extend(items)
            elif k in ["subtitle", "sub"] or k.startswith("subtitle") or k.startswith("sub"):
                res["subtitle"].extend(items)
        else:
            items = [x.strip() for x in line.split(",") if x.strip()]
            res["audio"].extend(items)
    return res


def match_stream_lang_or_pos(stream, stream_pos, user_items):
    if not user_items or not stream:
        return False
    lang = str(stream.get("tags", {}).get("language", "")).strip().lower()
    title = str(stream.get("tags", {}).get("title", "")).strip().lower()

    for item in user_items:
        item_str = str(item).strip().lower()
        if not item_str:
            continue
        if item_str.isdigit():
            val = int(item_str)
            if val == stream_pos or val == stream.get("index"):
                return True
            continue

        if lang:
            if item_str == lang or item_str in lang or lang in item_str:
                return True
            try:
                l1 = Language.get(lang)
                l2 = Language.get(item_str)
                if l1.language == l2.language:
                    return True
            except Exception:
                pass

        if title and (item_str in title):
            return True

    return False


def reorder_stream_list(streams, spec_str, target_type="audio"):
    if not streams or not spec_str:
        return streams

    type_streams = [s for s in streams if s.get("codec_type") == target_type]
    if len(type_streams) <= 1:
        return streams

    spec_for_type = spec_str
    if "aud=" in spec_str or "sub=" in spec_str or "audio=" in spec_str or "subtitle=" in spec_str:
        found_spec = []
        parts = [p.strip() for p in re.split(r"[|;\n]", spec_str) if p.strip()]
        for part in parts:
            if "=" in part:
                k, v = part.split("=", 1)
                k = k.strip().lower()
                if target_type == "audio" and k in ["audio", "aud"]:
                    found_spec.append(v.strip())
                elif target_type == "subtitle" and k in ["subtitle", "sub"]:
                    found_spec.append(v.strip())
        if found_spec:
            spec_for_type = ", ".join(found_spec)
        elif ("aud=" in spec_str or "audio=" in spec_str) and target_type == "subtitle":
            return streams
        elif ("sub=" in spec_str or "subtitle=" in spec_str) and target_type == "audio":
            return streams

    clean_spec = spec_for_type.strip()
    if clean_spec in ["1-2", "1:2", "1-2,"]:
        reordered_type = list(type_streams)
        reordered_type[0], reordered_type[1] = reordered_type[1], reordered_type[0]
        res = []
        t_idx = 0
        for s in streams:
            if s.get("codec_type") == target_type:
                res.append(reordered_type[t_idx])
                t_idx += 1
            else:
                res.append(s)
        return res

    items = [x.strip() for x in clean_spec.split(",") if x.strip()]
    placed = {}
    used_streams = set()

    for item in items:
        if ":" in item:
            pos_part, target_part = item.split(":", 1)
            pos_part = pos_part.strip()
            target_part = target_part.strip()
            if pos_part.isdigit():
                target_pos = int(pos_part) - 1
                for idx, s in enumerate(type_streams, 1):
                    if id(s) not in used_streams and match_stream_lang_or_pos(s, idx, [target_part]):
                        placed[target_pos] = s
                        used_streams.add(id(s))
                        break

    if not placed:
        return streams

    new_type_streams = []
    num_type_streams = len(type_streams)
    unused_streams = [s for s in type_streams if id(s) not in used_streams]

    for i in range(num_type_streams):
        if i in placed:
            new_type_streams.append(placed[i])
        else:
            if unused_streams:
                new_type_streams.append(unused_streams.pop(0))

    new_type_streams.extend(unused_streams)

    res = []
    t_idx = 0
    for s in streams:
        if s.get("codec_type") == target_type:
            res.append(new_type_streams[t_idx])
            t_idx += 1
        else:
            res.append(s)
    return res


class FFMpeg:
    def __init__(self, listener):
        self._listener = listener
        self._processed_bytes = 0
        self._last_processed_bytes = 0
        self._processed_time = 0
        self._last_processed_time = 0
        self._speed_raw = 0
        self._progress_raw = 0
        self._total_time = 0
        self._eta_raw = 0
        self._time_rate = 0.1
        self._start_time = 0

    @property
    def processed_bytes(self):
        return self._processed_bytes

    @property
    def speed_raw(self):
        return self._speed_raw

    @property
    def progress_raw(self):
        return self._progress_raw

    @property
    def eta_raw(self):
        return self._eta_raw

    async def get_streams(self, file):
        return await get_streams(file)

    def clear(self):
        self._start_time = time()
        self._processed_bytes = 0
        self._processed_time = 0
        self._speed_raw = 0
        self._progress_raw = 0
        self._eta_raw = 0
        self._time_rate = 0.1
        self._last_processed_time = 0
        self._last_processed_bytes = 0

    async def _ffmpeg_progress(self):
        while not (
            self._listener.subproc.returncode is not None
            or self._listener.is_cancelled
            or self._listener.subproc.stdout.at_eof()
        ):
            try:
                line = await wait_for(self._listener.subproc.stdout.readline(), 60)
            except Exception:
                break
            line = line.decode().strip()
            if not line:
                break
            if "=" in line:
                key, value = line.split("=", 1)
                if value != "N/A":
                    if key == "total_size":
                        self._processed_bytes = int(value) + self._last_processed_bytes
                        self._speed_raw = self._processed_bytes / (
                            time() - self._start_time
                        )
                    elif key == "speed":
                        self._time_rate = max(0.1, float(value.strip("x")))
                    elif key == "out_time":
                        self._processed_time = (
                            time_to_seconds(value) + self._last_processed_time
                        )
                        try:
                            self._progress_raw = (
                                self._processed_time * 100
                            ) / self._total_time
                            if (
                                hasattr(self._listener, "subsize")
                                and self._listener.subsize
                                and self._progress_raw > 0
                            ):
                                self._processed_bytes = int(
                                    self._listener.subsize * (self._progress_raw / 100)
                                )
                            if (time() - self._start_time) > 0:
                                self._speed_raw = self._processed_bytes / (
                                    time() - self._start_time
                                )
                            else:
                                self._speed_raw = 0
                            self._eta_raw = (
                                self._total_time - self._processed_time
                            ) / self._time_rate
                        except ZeroDivisionError:
                            self._progress_raw = 0
                            self._eta_raw = 0
            await sleep(0.05)

    async def ffmpeg_cmds(self, ffmpeg, f_path):
        self.clear()
        self._total_time = (await get_media_info(f_path))[0]
        base_name, ext = ospath.splitext(f_path)
        dir, base_name = base_name.rsplit("/", 1)
        indices = [
            index
            for index, item in enumerate(ffmpeg)
            if item.startswith("mltb") or item == "mltb"
        ]
        outputs = []
        for index in indices:
            output_file = ffmpeg[index]
            if output_file != "mltb" and output_file.startswith("mltb"):
                bo, oext = ospath.splitext(output_file)
                if oext:
                    if ext == oext:
                        prefix = f"ffmpeg{index}." if bo == "mltb" else ""
                    else:
                        prefix = ""
                    ext = ""
                else:
                    prefix = ""
            else:
                prefix = f"ffmpeg{index}."
            output = f"{dir}/{prefix}{output_file.replace('mltb', base_name)}{ext}"
            outputs.append(output)
            ffmpeg[index] = output
        if self._listener.is_cancelled:
            return False
        self._listener.subproc = await create_subprocess_exec(
            *ffmpeg, stdout=PIPE, stderr=PIPE
        )
        await self._ffmpeg_progress()
        _, stderr = await self._listener.subproc.communicate()
        code = self._listener.subproc.returncode
        if self._listener.is_cancelled:
            return False
        if code == 0:
            return outputs
        elif code == -9:
            self._listener.is_cancelled = True
            return False
        else:
            try:
                stderr = stderr.decode().strip()
            except Exception:
                stderr = "Unable to decode the error!"
            LOGGER.error(
                f"{stderr}. Something went wrong while running ffmpeg cmd, mostly file requires different/specific arguments. Path: {f_path}"
            )
            for op in outputs:
                if await aiopath.exists(op):
                    await remove(op)
            return False

    async def convert_video(self, video_file, ext, retry=False):
        cores, threads = ffmpeg_layout()
        self.clear()
        self._total_time = (await get_media_info(video_file))[0]
        base_name = ospath.splitext(video_file)[0]
        output = f"{base_name}.{ext}"
        if retry:
            cmd = [
                "taskset",
                "-c",
                f"{cores}",
                BinConfig.FFMPEG_NAME,
                "-hide_banner",
                "-loglevel",
                "error",
                "-progress",
                "pipe:1",
                "-i",
                video_file,
                "-map",
                "0",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                "-threads",
                f"{threads}",
                output,
            ]
            if ext == "mp4":
                cmd[17:17] = ["-c:s", "mov_text"]
            elif ext == "mkv":
                cmd[17:17] = ["-c:s", "ass"]
            else:
                cmd[17:17] = ["-c:s", "copy"]
        else:
            cmd = [
                "taskset",
                "-c",
                f"{cores}",
                BinConfig.FFMPEG_NAME,
                "-hide_banner",
                "-loglevel",
                "error",
                "-progress",
                "pipe:1",
                "-i",
                video_file,
                "-map",
                "0",
                "-c",
                "copy",
                "-threads",
                f"{threads}",
                output,
            ]
        if self._listener.is_cancelled:
            return False
        self._listener.subproc = await create_subprocess_exec(
            *cmd, stdout=PIPE, stderr=PIPE
        )
        await self._ffmpeg_progress()
        _, stderr = await self._listener.subproc.communicate()
        code = self._listener.subproc.returncode
        if self._listener.is_cancelled:
            return False
        if code == 0:
            return output
        elif code == -9:
            self._listener.is_cancelled = True
            return False
        else:
            if await aiopath.exists(output):
                await remove(output)
            if not retry:
                return await self.convert_video(video_file, ext, True)
            try:
                stderr = stderr.decode().strip()
            except Exception:
                stderr = "Unable to decode the error!"
            LOGGER.error(
                f"{stderr}. Something went wrong while converting video, mostly file need specific codec. Path: {video_file}"
            )
        return False

    async def convert_audio(self, audio_file, ext):
        cores, threads = ffmpeg_layout()
        self.clear()
        self._total_time = (await get_media_info(audio_file))[0]
        base_name = ospath.splitext(audio_file)[0]
        output = f"{base_name}.{ext}"
        cmd = [
            "taskset",
            "-c",
            f"{cores}",
            BinConfig.FFMPEG_NAME,
            "-hide_banner",
            "-loglevel",
            "error",
            "-progress",
            "pipe:1",
            "-i",
            audio_file,
            "-threads",
            f"{threads}",
            output,
        ]
        if self._listener.is_cancelled:
            return False
        self._listener.subproc = await create_subprocess_exec(
            *cmd, stdout=PIPE, stderr=PIPE
        )
        await self._ffmpeg_progress()
        _, stderr = await self._listener.subproc.communicate()
        code = self._listener.subproc.returncode
        if self._listener.is_cancelled:
            return False
        if code == 0:
            return output
        elif code == -9:
            self._listener.is_cancelled = True
            return False
        else:
            try:
                stderr = stderr.decode().strip()
            except Exception:
                stderr = "Unable to decode the error!"
            LOGGER.error(
                f"{stderr}. Something went wrong while converting audio, mostly file need specific codec. Path: {audio_file}"
            )
            if await aiopath.exists(output):
                await remove(output)
        return False

    async def sample_video(self, video_file, sample_duration, part_duration):
        cores, threads = ffmpeg_layout()
        self.clear()
        self._total_time = sample_duration
        dir, name = video_file.rsplit("/", 1)
        output_file = f"{dir}/SAMPLE.{name}"
        segments = [(0, part_duration)]
        duration = (await get_media_info(video_file))[0]
        remaining_duration = duration - (part_duration * 2)
        parts = (sample_duration - (part_duration * 2)) // part_duration
        time_interval = remaining_duration // parts
        next_segment = time_interval
        for _ in range(parts):
            segments.append((next_segment, next_segment + part_duration))
            next_segment += time_interval
        segments.append((duration - part_duration, duration))

        filter_complex = ""
        for i, (start, end) in enumerate(segments):
            filter_complex += (
                f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS[v{i}]; "
            )
            filter_complex += (
                f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{i}]; "
            )

        for i in range(len(segments)):
            filter_complex += f"[v{i}][a{i}]"

        filter_complex += f"concat=n={len(segments)}:v=1:a=1[vout][aout]"

        cmd = [
            "taskset",
            "-c",
            f"{cores}",
            BinConfig.FFMPEG_NAME,
            "-hide_banner",
            "-loglevel",
            "error",
            "-progress",
            "pipe:1",
            "-i",
            video_file,
            "-filter_complex",
            filter_complex,
            "-map",
            "[vout]",
            "-map",
            "[aout]",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-threads",
            f"{threads}",
            output_file,
        ]

        if self._listener.is_cancelled:
            return False
        self._listener.subproc = await create_subprocess_exec(
            *cmd, stdout=PIPE, stderr=PIPE
        )
        await self._ffmpeg_progress()
        _, stderr = await self._listener.subproc.communicate()
        code = self._listener.subproc.returncode
        if self._listener.is_cancelled:
            return False
        if code == -9:
            self._listener.is_cancelled = True
            return False
        elif code == 0:
            return output_file
        else:
            try:
                stderr = stderr.decode().strip()
            except Exception:
                stderr = "Unable to decode the error!"
            LOGGER.error(
                f"{stderr}. Something went wrong while creating sample video, mostly file is corrupted. Path: {video_file}"
            )
            if await aiopath.exists(output_file):
                await remove(output_file)
            return False

    async def split(self, f_path, file_, parts, split_size):
        cores, threads = ffmpeg_layout()
        self.clear()
        multi_streams = True
        self._total_time = duration = (await get_media_info(f_path))[0]
        base_name, extension = ospath.splitext(file_)
        split_size -= 3000000
        start_time = 0
        i = 1
        split_mode = getattr(self._listener, "split_mode", "part")
        digits = max(2, len(str(parts)))
        while i <= parts or start_time < duration - 4:
            suffix = f"part{i:0{digits}d}" if split_mode == "part" else f"{i:0{digits}d}"
            out_name = f"{base_name}.{suffix}{extension}"
            out_path = ospath.join(ospath.dirname(f_path), out_name)
            cmd = [
                "taskset",
                "-c",
                f"{cores}",
                BinConfig.FFMPEG_NAME,
                "-hide_banner",
                "-loglevel",
                "error",
                "-progress",
                "pipe:1",
                "-ss",
                str(start_time),
                "-i",
                f_path,
                "-fs",
                str(split_size),
                "-map",
                "0",
                "-map_chapters",
                "-1",
                "-async",
                "1",
                "-strict",
                "-2",
                "-c",
                "copy",
                "-threads",
                f"{threads}",
                out_path,
            ]
            if not multi_streams:
                del cmd[15]
                del cmd[15]
            if self._listener.is_cancelled:
                return False
            self._listener.subproc = await create_subprocess_exec(
                *cmd, stdout=PIPE, stderr=PIPE
            )
            await self._ffmpeg_progress()
            _, stderr = await self._listener.subproc.communicate()
            code = self._listener.subproc.returncode
            if self._listener.is_cancelled:
                return False
            if code == -9:
                self._listener.is_cancelled = True
                return False
            elif code != 0:
                try:
                    stderr = stderr.decode().strip()
                except Exception:
                    stderr = "Unable to decode the error!"
                with suppress(Exception):
                    await remove(out_path)
                if multi_streams:
                    LOGGER.warning(
                        f"{stderr}. Retrying without map, -map 0 not working in all situations. Path: {f_path}"
                    )
                    multi_streams = False
                    continue
                else:
                    LOGGER.warning(
                        f"{stderr}. Unable to split this video, if it's size less than {self._listener.max_split_size} will be uploaded as it is. Path: {f_path}"
                    )
                return False
            out_size = await aiopath.getsize(out_path)
            if out_size > self._listener.max_split_size:
                split_size -= (out_size - self._listener.max_split_size) + 5000000
                LOGGER.warning(
                    f"Part size is {out_size}. Trying again with lower split size!. Path: {f_path}"
                )
                await remove(out_path)
                continue
            lpd = (await get_media_info(out_path))[0]
            if lpd == 0:
                LOGGER.error(
                    f"Something went wrong while splitting, mostly file is corrupted. Path: {f_path}"
                )
                break
            elif duration == lpd:
                LOGGER.warning(
                    f"This file has been split with default stream and audio, so you will only see one part with less size from original one because it doesn't have all streams and audios. This happens mostly with MKV videos. Path: {f_path}"
                )
                break
            elif lpd <= 3:
                await remove(out_path)
                break
            self._last_processed_time += lpd
            self._last_processed_bytes += out_size
            start_time += lpd - 3
            i += 1
        return True

    async def encode_video(self, video_file, quality="", crf="", preset="", codec="", resolution="", fps=""):
        cores, threads = ffmpeg_layout()
        self.clear()
        self._total_time = (await get_media_info(video_file))[0]
        base_name, ext = ospath.splitext(video_file)
        output = f"{base_name}_encoded{ext}"

        cmd = [
            "taskset",
            "-c",
            f"{cores}",
            BinConfig.FFMPEG_NAME,
            "-hide_banner",
            "-loglevel",
            "error",
            "-progress",
            "pipe:1",
            "-i",
            video_file,
            "-map",
            "0",
        ]

        if codec:
            cmd.extend(["-c:v", codec])
        else:
            cmd.extend(["-c:v", "libx264"])

        cmd.extend(["-c:a", "copy", "-c:s", "copy"])

        if crf:
            cmd.extend(["-crf", str(crf)])
        if preset:
            cmd.extend(["-preset", str(preset)])
        if resolution and "x" in str(resolution):
            cmd.extend(["-s", str(resolution)])
        if fps:
            cmd.extend(["-r", str(fps)])

        cmd.extend(["-threads", f"{threads}", output])

        if self._listener.is_cancelled:
            return False
        self._listener.subproc = await create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
        await self._ffmpeg_progress()
        _, stderr = await self._listener.subproc.communicate()
        code = self._listener.subproc.returncode

        if code == 0 and await aiopath.exists(output):
            return output
        if await aiopath.exists(output):
            await remove(output)
        return False

    async def compress_video(self, video_file, quality="", crf="", preset="", audio_bitrate="", audio_codec=""):
        cores, threads = ffmpeg_layout()
        self.clear()
        self._total_time = (await get_media_info(video_file))[0]
        base_name, ext = ospath.splitext(video_file)
        output = f"{base_name}_compressed{ext}"

        cmd = [
            "taskset",
            "-c",
            f"{cores}",
            BinConfig.FFMPEG_NAME,
            "-hide_banner",
            "-loglevel",
            "error",
            "-progress",
            "pipe:1",
            "-i",
            video_file,
            "-map",
            "0",
            "-c:v",
            "libx264",
        ]

        cmd.extend(["-crf", str(crf) if crf else "28"])
        cmd.extend(["-preset", str(preset) if preset else "faster"])

        if audio_codec:
            cmd.extend(["-c:a", str(audio_codec)])
        else:
            cmd.extend(["-c:a", "aac"])

        if audio_bitrate:
            cmd.extend(["-b:a", str(audio_bitrate)])

        cmd.extend(["-c:s", "copy", "-threads", f"{threads}", output])

        if self._listener.is_cancelled:
            return False
        self._listener.subproc = await create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
        await self._ffmpeg_progress()
        _, stderr = await self._listener.subproc.communicate()
        code = self._listener.subproc.returncode

        if code == 0 and await aiopath.exists(output):
            return output
        if await aiopath.exists(output):
            await remove(output)
        return False

    async def apply_watermark(self, video_file, text="", image_path="", position="Top-Left", color="white"):
        cores, threads = ffmpeg_layout()
        self.clear()
        self._total_time = (await get_media_info(video_file))[0]
        base_name, ext = ospath.splitext(video_file)
        output = f"{base_name}_wm{ext}"

        pos_map_text = {
            "Top-Left": "x=10:y=10",
            "Top-Center": "x=(w-text_w)/2:y=10",
            "Top-Right": "x=w-text_w-10:y=10",
            "Center-Left": "x=10:y=(h-text_h)/2",
            "Center": "x=(w-text_w)/2:y=(h-text_h)/2",
            "Center-Right": "x=w-text_w-10:y=(h-text_h)/2",
            "Bottom-Left": "x=10:y=h-text_h-10",
            "Bottom-Center": "x=(w-text_w)/2:y=h-text_h-10",
            "Bottom-Right": "x=w-text_w-10:y=h-text_h-10",
        }

        pos_map_img = {
            "Top-Left": "10:10",
            "Top-Center": "(main_w-overlay_w)/2:10",
            "Top-Right": "main_w-overlay_w-10:10",
            "Center-Left": "10:(main_h-overlay_h)/2",
            "Center": "(main_w-overlay_w)/2:(main_h-overlay_h)/2",
            "Center-Right": "main_w-overlay_w-10:(main_h-overlay_h)/2",
            "Bottom-Left": "10:main_h-overlay_h-10",
            "Bottom-Center": "(main_w-overlay_w)/2:main_h-overlay_h-10",
            "Bottom-Right": "main_w-overlay_w-10:main_h-overlay_h-10",
        }

        if image_path and await aiopath.exists(image_path):
            overlay_pos = pos_map_img.get(position, "10:10")
            filter_str = f"[0:v][1:v]overlay={overlay_pos}[outv]"
            cmd = [
                "taskset", "-c", f"{cores}", BinConfig.FFMPEG_NAME,
                "-hide_banner", "-loglevel", "error", "-progress", "pipe:1",
                "-i", video_file, "-i", image_path,
                "-filter_complex", filter_str,
                "-map", "[outv]", "-map", "0:a?", "-map", "0:s?",
                "-c:v", "libx264", "-c:a", "copy", "-c:s", "copy",
                "-threads", f"{threads}", output
            ]
        elif text:
            escaped_text = text.replace(":", r"\:").replace("'", r"'\''")
            text_pos = pos_map_text.get(position, "x=10:y=10")
            font_color = color or "white"
            vf = f"drawtext=text='{escaped_text}':fontcolor={font_color}:fontsize=24:{text_pos}"
            cmd = [
                "taskset", "-c", f"{cores}", BinConfig.FFMPEG_NAME,
                "-hide_banner", "-loglevel", "error", "-progress", "pipe:1",
                "-i", video_file,
                "-vf", vf,
                "-map", "0", "-c:v", "libx264", "-c:a", "copy", "-c:s", "copy",
                "-threads", f"{threads}", output
            ]
        else:
            return video_file

        if self._listener.is_cancelled:
            return False
        self._listener.subproc = await create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
        await self._ffmpeg_progress()
        _, stderr = await self._listener.subproc.communicate()
        code = self._listener.subproc.returncode

        if code == 0 and await aiopath.exists(output):
            return output
        if await aiopath.exists(output):
            await remove(output)
        return False

    async def merge_tracks(self, video_files, audio_files, sub_files, output_file, gid, a_langs=None, s_langs=None):
        cores, threads = ffmpeg_layout()
        self.clear()
        a_langs = a_langs or []
        s_langs = s_langs or []

        total_dur = 0
        all_inputs = video_files + audio_files + sub_files
        for f in all_inputs:
            dur = (await get_media_info(f))[0]
            if dur > total_dur:
                total_dur = dur
        self._total_time = total_dur or 1

        cmd = [
            "taskset",
            "-c",
            f"{cores}",
            BinConfig.FFMPEG_NAME,
            "-hide_banner",
            "-loglevel",
            "error",
            "-progress",
            "pipe:1",
        ]

        list_file_path = None

        if len(video_files) > 1:
            list_file_path = f"{output_file}.txt"
            async with aiopen(list_file_path, "w") as f:
                for vf in video_files:
                    escaped_vf = vf.replace("'", r"'\''")
                    await f.write(f"file '{escaped_vf}'\n")
            cmd.extend(["-f", "concat", "-safe", "0", "-i", list_file_path])
        elif len(video_files) == 1:
            cmd.extend(["-i", video_files[0]])

        for af in audio_files:
            cmd.extend(["-i", af])

        for sf in sub_files:
            cmd.extend(["-i", sf])

        total_streams_num = (1 if len(video_files) > 1 else len(video_files)) + len(audio_files) + len(sub_files)

        if len(video_files) > 0:
            cmd.extend(["-map", "0:v?", "-map", "0:a?", "-map", "0:s?"])
            for idx in range(1, total_streams_num):
                cmd.extend(["-map", f"{idx}:a?", "-map", f"{idx}:s?"])
        else:
            for idx in range(total_streams_num):
                cmd.extend(["-map", f"{idx}"])

        for idx, alang in enumerate(a_langs):
            if alang:
                cmd.extend([f"-metadata:s:a:{idx}", f"language={alang}", f"-metadata:s:a:{idx}", f"title={alang}"])
        for idx, slang in enumerate(s_langs):
            if slang:
                cmd.extend([f"-metadata:s:s:{idx}", f"language={slang}", f"-metadata:s:s:{idx}", f"title={slang}"])

        cmd.extend(["-c", "copy", "-threads", f"{threads}", output_file])

        if self._listener.is_cancelled:
            if list_file_path:
                with suppress(Exception):
                    await remove(list_file_path)
            return False

        self._listener.subproc = await create_subprocess_exec(
            *cmd, stdout=PIPE, stderr=PIPE
        )
        await self._ffmpeg_progress()
        _, stderr = await self._listener.subproc.communicate()
        code = self._listener.subproc.returncode

        if list_file_path:
            with suppress(Exception):
                await remove(list_file_path)

        if self._listener.is_cancelled:
            return False
        if code == 0:
            return output_file
        elif code == -9:
            self._listener.is_cancelled = True
            return False
        else:
            if await aiopath.exists(output_file):
                await remove(output_file)
            try:
                stderr = stderr.decode().strip()
            except Exception:
                stderr = "Unable to decode the error!"
            LOGGER.error(
                f"{stderr}. Something went wrong while merging tracks. Output: {output_file}"
            )
            return False

    async def merge_videos(self, video_files, output_file, gid):
        return await self.merge_tracks(video_files, [], [], output_file, gid)

    async def auto_remove_kept(self, f_path, kept_spec):
        if not f_path or not await aiopath.exists(f_path):
            return f_path
        parsed = parse_track_spec(kept_spec)
        kept_aud = parsed.get("audio", [])
        kept_sub = parsed.get("subtitle", [])

        if not kept_aud and not kept_sub:
            return f_path

        streams = await self.get_streams(f_path)
        if not streams or len(streams) <= 1:
            return f_path

        v_streams = [s for s in streams if s.get("codec_type") == "video"]
        a_streams = [s for s in streams if s.get("codec_type") == "audio"]
        s_streams = [s for s in streams if s.get("codec_type") == "subtitle"]
        o_streams = [s for s in streams if s.get("codec_type") not in ["video", "audio", "subtitle"]]

        retained_a = a_streams
        if kept_aud and a_streams:
            matched_a = [s for idx, s in enumerate(a_streams, 1) if match_stream_lang_or_pos(s, idx, kept_aud)]
            if matched_a:
                retained_a = matched_a

        retained_s = s_streams
        if kept_sub and s_streams:
            matched_s = [s for idx, s in enumerate(s_streams, 1) if match_stream_lang_or_pos(s, idx, kept_sub)]
            retained_s = matched_s

        retained_streams = v_streams + retained_a + retained_s + o_streams
        if len(retained_streams) == len(streams):
            return f_path

        out_path = f"{f_path}.kept.mkv"
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", f_path]
        for st in retained_streams:
            cmd.extend(["-map", f"0:{st.get('index')}"])
        cmd.extend(["-c", "copy", out_path])

        res, err, code = await cmd_exec(cmd)
        if code == 0 and await aiopath.exists(out_path):
            await remove(f_path)
            await move(out_path, f_path)
            return f_path
        return f_path

    async def auto_remove_remove(self, f_path, remove_spec):
        if not f_path or not await aiopath.exists(f_path):
            return f_path
        parsed = parse_track_spec(remove_spec)
        rm_aud = parsed.get("audio", [])
        rm_sub = parsed.get("subtitle", [])

        if not rm_aud and not rm_sub:
            return f_path

        streams = await self.get_streams(f_path)
        if not streams or len(streams) <= 1:
            return f_path

        v_streams = [s for s in streams if s.get("codec_type") == "video"]
        a_streams = [s for s in streams if s.get("codec_type") == "audio"]
        s_streams = [s for s in streams if s.get("codec_type") == "subtitle"]
        o_streams = [s for s in streams if s.get("codec_type") not in ["video", "audio", "subtitle"]]

        retained_a = a_streams
        if rm_aud and a_streams:
            retained_a = [s for idx, s in enumerate(a_streams, 1) if not match_stream_lang_or_pos(s, idx, rm_aud)]

        retained_s = s_streams
        if rm_sub and s_streams:
            retained_s = [s for idx, s in enumerate(s_streams, 1) if not match_stream_lang_or_pos(s, idx, rm_sub)]

        retained_streams = v_streams + retained_a + retained_s + o_streams
        if len(retained_streams) == len(streams) or not retained_streams:
            return f_path

        out_path = f"{f_path}.rm.mkv"
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", f_path]
        for st in retained_streams:
            cmd.extend(["-map", f"0:{st.get('index')}"])
        cmd.extend(["-c", "copy", out_path])

        res, err, code = await cmd_exec(cmd)
        if code == 0 and await aiopath.exists(out_path):
            await remove(f_path)
            await move(out_path, f_path)
            return f_path
        return f_path

    async def auto_remove_reorder(self, f_path, reorder_spec):
        if not f_path or not await aiopath.exists(f_path):
            return f_path
        streams = await self.get_streams(f_path)
        if not streams or len(streams) <= 1:
            return f_path

        reordered = reorder_stream_list(streams, reorder_spec, target_type="audio")
        reordered = reorder_stream_list(reordered, reorder_spec, target_type="subtitle")

        if [s.get("index") for s in reordered] == [s.get("index") for s in streams]:
            return f_path

        out_path = f"{f_path}.reorder.mkv"
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", f_path]
        for st in reordered:
            cmd.extend(["-map", f"0:{st.get('index')}"])
        cmd.extend(["-c", "copy", out_path])

        res, err, code = await cmd_exec(cmd)
        if code == 0 and await aiopath.exists(out_path):
            await remove(f_path)
            await move(out_path, f_path)
            return f_path
        return f_path

    async def audio_split(self, f_path, split_spec):
        if not f_path or not await aiopath.exists(f_path) or not split_spec:
            return False
        split_items = [x.strip() for x in split_spec.split(",") if x.strip()]
        if not split_items:
            return False

        streams = await self.get_streams(f_path)
        if not streams:
            return False

        a_streams = [s for s in streams if s.get("codec_type") == "audio"]
        if not a_streams:
            return False

        dir_path, filename = ospath.split(f_path)
        base_name, _ = ospath.splitext(filename)
        created = False

        for idx, s in enumerate(a_streams, 1):
            if match_stream_lang_or_pos(s, idx, split_items):
                lang = s.get("tags", {}).get("language", f"track{idx}")
                out_name = f"{base_name}_{lang}_{idx}.mka"
                out_path = ospath.join(dir_path, out_name)

                cmd = [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    f_path,
                    "-map",
                    f"0:{s.get('index')}",
                    "-c",
                    "copy",
                    out_path,
                ]
                res, err, code = await cmd_exec(cmd)
                if code == 0 and await aiopath.exists(out_path):
                    created = True

        return created

    async def trim_media(self, f_path, start_time, end_time):
        out_path = f"{f_path}.trim.mkv"
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            start_time,
            "-to",
            end_time,
            "-i",
            f_path,
            "-c",
            "copy",
            out_path,
        ]
        res, err, code = await cmd_exec(cmd)
        if code == 0 and await aiopath.exists(out_path):
            await remove(f_path)
            await move(out_path, f_path)
            return f_path
        return None

    async def extract_tracks(self, f_path, extract_types):
        streams = await self.get_streams(f_path)
        if not streams:
            return None
        dir_name = ospath.dirname(f_path)
        base_name = ospath.splitext(ospath.basename(f_path))[0]

        for st in streams:
            st_type = st.get("codec_type")
            st_idx = st.get("index")
            lang = st.get("tags", {}).get("language", "und")

            if st_type == "video" and ("video" in extract_types or "file" in extract_types):
                ext = st.get("codec_name", "mkv")
                out_file = ospath.join(dir_name, f"{base_name}_video_{st_idx}_{lang}.{ext}")
                cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", f_path, "-map", f"0:{st_idx}", "-c", "copy", out_file]
                await cmd_exec(cmd)
            elif st_type == "audio" and "audio" in extract_types:
                ext = st.get("codec_name", "m4a")
                out_file = ospath.join(dir_name, f"{base_name}_audio_{st_idx}_{lang}.{ext}")
                cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", f_path, "-map", f"0:{st_idx}", "-c", "copy", out_file]
                await cmd_exec(cmd)
            elif st_type == "subtitle" and "subtitle" in extract_types:
                ext = "srt" if st.get("codec_name") in ["subrip", "srt"] else "ass"
                out_file = ospath.join(dir_name, f"{base_name}_sub_{st_idx}_{lang}.{ext}")
                cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", f_path, "-map", f"0:{st_idx}", "-c", "copy", out_file]
                await cmd_exec(cmd)

        return f_path
