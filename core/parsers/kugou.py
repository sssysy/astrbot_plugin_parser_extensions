"""酷狗音乐解析器 - 基于 NodeJS 酷狗音乐 API (KuGouMusicApi)"""

import asyncio
import base64
import datetime
import re
import time
from collections.abc import AsyncGenerator
from re import Match
from typing import Any, ClassVar

from pathlib import Path

from aiohttp import ClientError

from data.plugins.astrbot_plugin_parser.core.cookie import CookieJar
from data.plugins.astrbot_plugin_parser.core.data import (
    AudioContent,
    ImageContent,
    MediaContent,
    Platform,
    SendGroup,
    TextContent,
)
from data.plugins.astrbot_plugin_parser.core.download import Downloader
from data.plugins.astrbot_plugin_parser.core.exception import ParseException
from data.plugins.astrbot_plugin_parser.core.parsers.base import BaseParser, handle

from ..config import PluginConfig

# 音质降级梯队，自高向低排列
QUALITY_FALLBACK_ORDER = [
    "viper_atmos",
    "viper_clear",
    "high",
    "flac",
    "320",
    "128",
]


class KuGouParser(BaseParser):
    """酷狗音乐解析器"""

    platform: ClassVar[Platform] = Platform(name="kugou", display_name="酷狗音乐")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = getattr(config.parser, "kugou", None)
        self.base_url = (getattr(self.mycfg, "base_url", None) or "http://localhost:3000").rstrip("/")
        self.quality = getattr(self.mycfg, "quality", None) or "320"
        self.cookiejar = CookieJar(config, self.mycfg, domain="kugou.com")
        self._qr_key: str | None = None

        if not self.cookiejar.cookies_str:
            docs_cookie = Path(__file__).resolve().parent.parent.parent / "docs" / "kgcookie.txt"
            if docs_cookie.exists():
                raw = docs_cookie.read_text(encoding="utf-8").strip()
                if raw:
                    self.cookiejar.cookies_str = self.cookiejar.clean_cookies_str(raw)
                    self.cookiejar._load_from_cookies_str(self.cookiejar.cookies_str)
                    self.cookiejar.save_to_file()

        self.headers.update({"Referer": "https://www.kugou.com"})
        self._sync_cookie_header()

    def _sync_cookie_header(self) -> None:
        """同步 cookie 到请求头"""
        cookie_str = self.cookiejar.cookies_str or ""
        self.headers["cookie"] = cookie_str

    # ==================== 链接匹配 ====================

    # 1. 移动端/短链重定向
    @handle("t1.kugou.com", r"t1\.kugou\.com/(?P<key>[a-zA-Z0-9]+)")
    @handle("t2.kugou.com", r"t2\.kugou\.com/(?P<key>[a-zA-Z0-9]+)")
    @handle("t3.kugou.com", r"t3\.kugou\.com/(?P<key>[a-zA-Z0-9]+)")
    @handle("t4.kugou.com", r"t4\.kugou\.com/(?P<key>[a-zA-Z0-9]+)")
    @handle("t5.kugou.com", r"t5\.kugou\.com/(?P<key>[a-zA-Z0-9]+)")
    @handle("t6.kugou.com", r"t6\.kugou\.com/(?P<key>[a-zA-Z0-9]+)")
    @handle("t7.kugou.com", r"t7\.kugou\.com/(?P<key>[a-zA-Z0-9]+)")
    @handle("t8.kugou.com", r"t8\.kugou\.com/(?P<key>[a-zA-Z0-9]+)")
    @handle("t9.kugou.com", r"t9\.kugou\.com/(?P<key>[a-zA-Z0-9]+)")
    @handle("t.kugou.com", r"t\.kugou\.com/(?P<key>[a-zA-Z0-9]+)")
    async def _parse_short(self, searched: Match[str]):
        matched = searched.group(0)
        full_url = matched if matched.startswith("http") else f"https://{matched}"
        return await self.parse_with_redirect(full_url)

    # 2. 移动端分享链重定向
    @handle("m.kugou.com/share", r"(?i)m\.kugou\.com/share/?.*[?&]chain=(?P<chain>[a-zA-Z0-9]+)")
    @handle("h5.kugou.com/share", r"(?i)h5\.kugou\.com/share/?.*[?&]chain=(?P<chain>[a-zA-Z0-9]+)")
    @handle("kugou.com/share", r"(?i)kugou\.com/share/?.*[?&]chain=(?P<chain>[a-zA-Z0-9]+)")
    async def _parse_share(self, searched: Match[str]):
        chain = searched.group("chain")
        return await self.parse_with_redirect(f"https://m.kugou.com/share/?chain={chain}")

    # 3. 电脑/网页端/H5单曲播放页（URL 中含有 hash 或歌曲 ID）
    @handle("kugou.com/song", r"(?i)kugou\.com/song/?(?:index\.php)?.*[#?&](?:hash|file_?hash|song_?hash)=(?P<hash>[a-f0-9]{32})")
    @handle("h5.kugou.com", r"(?i)h5\.kugou\.com/.*[#?&](?:hash|file_?hash|song_?hash|album_audio_id|mix_?song_?id|audio_id)=(?P<key>[a-zA-Z0-9]+)")
    async def _parse_song_hash(self, searched: Match[str]):
        # 1. 尝试从 URL 中直接提取 32 位 hash
        hash_val = ""
        if hash_m := re.search(r"(?i)(?:hash|file_?hash|song_?hash)=([a-f0-9]{32})", searched.string):
            hash_val = hash_m.group(1).lower()

        # 2. 尝试提取 album_id 与 album_audio_id
        album_id = 0
        if album_m := re.search(r"album_id=(\d+)", searched.string):
            album_id = int(album_m.group(1))

        album_audio_id = 0
        if aaid_m := re.search(r"(?i)(?:album_audio_id|mix_?song_?id|audio_id)=(\d+)", searched.string):
            album_audio_id = int(aaid_m.group(1))

        # 3. 若无 hash 但有 album_audio_id，通过 /krm/audio 查询 hash
        if not hash_val and album_audio_id > 0:
            krm_info = await self._get_krm_audio(album_audio_id)
            hash_val = krm_info.get("hash", "").lower()

        if not hash_val:
            raise ParseException("未能从酷狗链接中提取到歌曲哈希 (hash)")

        return await self._process_song(hash_val, album_id=album_id, album_audio_id=album_audio_id)

    # 4. 手机端播放页
    @handle("m.kugou.com/play/info", r"m\.kugou\.com/play/info/(?P<hash>[a-fA-F0-9]{32})")
    @handle("h5.kugou.com/play/info", r"h5\.kugou\.com/play/info/(?P<hash>[a-fA-F0-9]{32})")
    async def _parse_play_info(self, searched: Match[str]):
        hash_val = searched.group("hash").lower()
        return await self._process_song(hash_val)

    # 5. 电脑/移动端 mixsong 页面
    @handle("kugou.com/mixsong", r"kugou\.com/mixsong/(?P<mix_id>[a-zA-Z0-9]+)\.html")
    async def _parse_mixsong(self, searched: Match[str]):
        mix_id = searched.group("mix_id")
        return await self._process_mixsong(mix_id)

    # 6. 直链音频匹配
    @handle("kugou.com", r"https?://[^/]*\.kugou\.com/.*\.mp3(?:\?.*)?$")
    async def _parse_direct_mp3(self, searched: Match[str]):
        url = searched.group(0)
        audio = self.create_audio_content(url)
        return self.result(
            title="酷狗音乐",
            text="直链音频",
            contents=[audio],
            url=url,
        )

    # ==================== 扫码登录 ====================

    async def login_with_qrcode(self) -> bytes:
        """获取登录二维码图片数据（返回 PNG 二进制 bytes）"""
        ts = int(time.time() * 1000)

        # Step 1: 获取二维码 key
        key_url = f"{self.base_url}/login/qr/key?timestamp={ts}"
        try:
            async with self.session.get(key_url, headers=self.headers) as resp:
                if resp.status >= 400:
                    raise ParseException(f"获取酷狗二维码 key 失败: HTTP {resp.status}")
                key_data = await resp.json()
        except ClientError as e:
            raise ParseException(f"连接酷狗 API 服务失败: {e}") from e

        data_obj = key_data.get("data")
        unikey = None
        if isinstance(data_obj, dict):
            unikey = data_obj.get("qrcode") or data_obj.get("key")
        elif isinstance(data_obj, str):
            unikey = data_obj
        if not unikey:
            unikey = key_data.get("qrcode")

        if not unikey:
            raise ParseException(f"未能获取到酷狗二维码 key: {key_data}")
        self._qr_key = unikey

        # Step 2: 获取二维码 Base64 图片
        qr_url = f"{self.base_url}/login/qr/create?key={unikey}&qrimg=true&timestamp={ts}"
        try:
            async with self.session.get(qr_url, headers=self.headers) as resp:
                if resp.status >= 400:
                    raise ParseException(f"获取酷狗二维码图片失败: HTTP {resp.status}")
                qr_data = await resp.json()
        except ClientError as e:
            raise ParseException(f"连接酷狗 API 服务失败: {e}") from e

        qr_img = qr_data.get("data", {}).get("base64", "")
        if not qr_img:
            raise ParseException(f"未能生成二维码图片: {qr_data}")

        if qr_img.startswith("data:"):
            qr_img = qr_img.split(",", 1)[1]
        return base64.b64decode(qr_img)

    async def check_qr_state(self) -> AsyncGenerator[str, None]:
        """轮询二维码扫码状态，yield 状态消息"""
        if not self._qr_key:
            yield "未找到二维码 key，请重新生成"
            return

        for _ in range(60):
            ts = int(time.time() * 1000)
            check_url = f"{self.base_url}/login/qr/check?key={self._qr_key}&timestamp={ts}"
            try:
                async with self.session.get(check_url, headers=self.headers) as resp:
                    if resp.status >= 400:
                        await asyncio.sleep(3)
                        continue
                    check_data = await resp.json()
                    cookies_headers = resp.headers.getall("Set-Cookie", [])
            except Exception:
                await asyncio.sleep(3)
                continue

            # 状态判定：0 为过期，1 为等待扫码，2 为待确认，4 为授权登录成功
            data_info = check_data.get("data") or {}
            status_code = data_info.get("status") if isinstance(data_info, dict) else None
            if status_code is None:
                status_code = check_data.get("status")

            if status_code == 0:
                yield "二维码已过期，请重新生成"
                return
            elif status_code == 1:
                await asyncio.sleep(3)
                continue
            elif status_code == 2:
                yield "已扫码，请在手机上确认授权"
                await asyncio.sleep(3)
                continue
            elif status_code == 4:
                # 提取并保存 Cookie
                cookie_parts: list[str] = []
                token = data_info.get("token")
                userid = data_info.get("userid")
                if token:
                    cookie_parts.append(f"token={token}")
                if userid:
                    cookie_parts.append(f"userid={userid}")

                for sc in cookies_headers:
                    first_part = sc.split(";", 1)[0].strip()
                    if first_part and first_part not in cookie_parts:
                        cookie_parts.append(first_part)

                combined_cookie = "; ".join(cookie_parts)
                if combined_cookie:
                    self._save_cookies(combined_cookie)
                yield "酷狗音乐登录成功！"
                return
            else:
                await asyncio.sleep(3)
                continue

        yield "登录超时，请重新生成二维码"

    def _save_cookies(self, cookie_str: str) -> None:
        """保存 cookies 到本地并更新配置"""
        self.cookiejar._load_from_cookies_str(cookie_str)
        self.cookiejar.save_to_file()
        self.cookiejar.cookies_str = self.cookiejar.clean_cookies_str(cookie_str)
        self._sync_cookie_header()
        if self.mycfg:
            self.mycfg.cookies = self.cookiejar.cookies_str
        try:
            self.cfg.save_config()
        except Exception:
            pass

    # ==================== VIP 领取 ====================

    async def claim_vip(self) -> str:
        """手动领取酷狗概念版 1 天 VIP 并自动升级为畅听 VIP（固定获取当天一天）"""
        if not (self.cookiejar.cookies_str or (self.mycfg and getattr(self.mycfg, "cookies", None))):
            return "未检测到酷狗登录信息，请先使用【/登录酷狗】进行扫码登录，或在配置中填写 Cookies。"

        today_str = datetime.date.today().strftime("%Y-%m-%d")
        ts = int(time.time() * 1000)

        results: list[str] = [f"【酷狗音乐概念版 VIP 领取】", f"📅 日期：{today_str}"]

        # 1. 领取一天 VIP
        day_vip_url = f"{self.base_url}/youth/day/vip?receive_day={today_str}&timestamp={ts}"
        try:
            async with self.session.get(day_vip_url, headers=self.headers) as resp:
                day_res = await resp.json()
                day_msg = self._extract_api_message(day_res)
                results.append(f"🎁 1天VIP领取：{day_msg}")
        except Exception as e:
            results.append(f"🎁 1天VIP领取失败：{e}")

        # 2. 升级为畅听 VIP
        upgrade_url = f"{self.base_url}/youth/day/vip/upgrade?timestamp={ts + 1}"
        try:
            async with self.session.get(upgrade_url, headers=self.headers) as resp:
                up_res = await resp.json()
                up_msg = self._extract_api_message(up_res)
                results.append(f"⚡ 畅听VIP升级：{up_msg}")
        except Exception as e:
            results.append(f"⚡ 畅听VIP升级失败：{e}")

        # 3. 获取当月已领记录
        record_url = f"{self.base_url}/youth/month/vip/record?timestamp={ts + 2}"
        try:
            async with self.session.get(record_url, headers=self.headers) as resp:
                rec_res = await resp.json()
                record_data = rec_res.get("data")
                if isinstance(record_data, list):
                    results.append(f"📊 当月已领天数：{len(record_data)} 天")
                elif isinstance(record_data, dict) and "total" in record_data:
                    results.append(f"📊 当月已领天数：{record_data['total']} 天")
        except Exception:
            pass

        results.append("💡 提示：该福利仅限酷狗概念版账号，请确保 KuGouMusicApi 配置为 platform=lite。")
        return "\n".join(results)

    @staticmethod
    def _extract_api_message(res: dict[str, Any]) -> str:
        """提取 API 返回的可读信息"""
        if not isinstance(res, dict):
            return str(res)
        msg = res.get("msg") or res.get("error_msg") or res.get("message")
        status = res.get("status")
        error_code = res.get("error_code")
        if msg:
            return str(msg)
        if error_code == 0 or status == 1:
            return "成功"
        return f"返回状态码: status={status}, error_code={error_code}"

    # ==================== 歌曲处理核心 ====================

    async def _process_mixsong(self, mix_id: str):
        """处理 mixsong 页面（提取 hash 后转入单曲解析）"""
        url = f"https://www.kugou.com/mixsong/{mix_id}.html"
        html_text = ""
        try:
            async with self.session.get(url, headers=self.headers, proxy=self.proxy) as resp:
                if resp.status < 400:
                    html_text = await resp.text()
        except Exception:
            pass

        hash_val = ""
        album_audio_id = 0
        if html_text:
            if hash_m := re.search(r'"(?:hash|FileHash|file_hash)"\s*:\s*"(?P<hash>[a-fA-F0-9]{32})"', html_text, re.I):
                hash_val = hash_m.group("hash").lower()
            if aaid_m := re.search(r'"(?:album_audio_id|mixsongid)"\s*:\s*(\d+)', html_text, re.I):
                album_audio_id = int(aaid_m.group(1))

        if not hash_val and mix_id.isdigit():
            album_audio_id = int(mix_id)

        # 若依然无法拿到 hash，尝试从 /krm/audio 获取详情
        if not hash_val and album_audio_id > 0:
            krm_info = await self._get_krm_audio(album_audio_id)
            hash_val = krm_info.get("hash", "").lower()

        if not hash_val:
            raise ParseException(f"未能从酷狗 mixsong 页面提取到歌曲哈希: {mix_id}")

        return await self._process_song(hash_val, album_audio_id=album_audio_id)

    async def _process_song(self, hash_val: str, album_id: int = 0, album_audio_id: int = 0):
        """处理单曲解析"""
        # 1. 获取歌曲详情
        detail = await self._get_song_detail(hash_val)
        if not detail and album_audio_id > 0:
            detail = await self._get_krm_audio(album_audio_id)

        audio_name = detail.get("audio_name") or ""
        song_name = detail.get("song_name") or detail.get("songname") or ""
        artist_name = detail.get("author_name") or detail.get("singer_name") or ""
        album_name = detail.get("album_name") or ""

        if not song_name and audio_name:
            if " - " in audio_name:
                parts = audio_name.split(" - ", 1)
                artist_name = artist_name or parts[0].strip()
                song_name = parts[1].strip()
            else:
                song_name = audio_name

        song_name = song_name or "未知歌曲"
        artist_name = artist_name or "未知歌手"
        # 过滤文件名中不合法字符，防止写入本地或发送时报错
        clean_song_name = re.sub(r'[\\/:*?"<>|]', " ", song_name).strip()
        clean_artist_name = re.sub(r'[\\/:*?"<>|]', " & ", artist_name).strip()
        title = song_name

        # 时长解析：安全转换为整数，毫秒转秒
        raw_timelength = detail.get("timelength") or detail.get("duration") or 0
        try:
            timelength = int(float(raw_timelength))
        except (ValueError, TypeError):
            timelength = 0
        duration_sec = (timelength // 1000) if timelength > 10000 else timelength

        try:
            album_id = album_id or int(float(detail.get("album_id") or 0))
        except (ValueError, TypeError):
            pass

        try:
            album_audio_id = album_audio_id or int(float(detail.get("album_audio_id") or detail.get("mixsongid") or 0))
        except (ValueError, TypeError):
            pass

        # 2. 获取封面与头像图片
        cover_url, avatar_url = await self._get_song_images(
            hash_val, album_id=album_id, album_audio_id=album_audio_id
        )

        # 3. 获取播放 URL（直接按指定音质请求，不降级兜底）
        url_info = await self._query_song_url(
            hash_val, self.quality, album_id=album_id, album_audio_id=album_audio_id
        )
        audio_url = url_info["url"]
        file_size = url_info["file_size"]
        audio_type = url_info["extname"]
        audio_hash = url_info["hash"]
        resolved_quality = self.quality

        # 4. 创建音频与封面内容
        safe_filename = f"{clean_song_name} - {clean_artist_name}.{audio_type}"
        audio_task = self.downloader.download_audio(
            audio_url,
            audio_name=safe_filename,
            headers=self.downloader.default_headers,
            proxy=self.proxy,
        )
        audio_content = AudioContent(audio_task, duration=duration_sec)
        author = self.create_author(artist_name, avatar_url=avatar_url)

        card_contents: list[MediaContent] = []
        if cover_url:
            cover_task = self.downloader.download_img(
                cover_url, headers=self.downloader.default_headers, proxy=self.proxy
            )
            card_contents.append(ImageContent(cover_task))

        # 5. 元数据与发送策略构建
        minutes, seconds = divmod(duration_sec, 60)
        meta_parts = [f"音质: {resolved_quality}", f"时长: {minutes}:{seconds:02d}"]
        if file_size:
            size_mb = file_size / (1024 * 1024)
            meta_parts.append(f"{size_mb:.1f}MB" if size_mb >= 1 else f"{file_size / 1024:.0f}KB")
        extra = {"info": " | ".join(meta_parts)}
        text = f"专辑: {album_name}" if album_name else None

        if self.cfg.single_heavy_render_card:
            send_groups = [
                SendGroup(contents=[], render_card=True, force_merge=False),
                SendGroup(contents=[audio_content], render_card=False, force_merge=False),
            ]
        else:
            send_text = f"{title} - {artist_name}"
            if text:
                send_text += f"\n{text}"
            send_groups = [
                SendGroup(
                    contents=[audio_content, TextContent(send_text)],
                    render_card=False,
                    force_merge=False,
                )
            ]

        return self.result(
            title=title,
            text=text,
            author=author,
            contents=card_contents,
            send_groups=send_groups,
            extra=extra,
            url=f"https://www.kugou.com/song/#hash={hash_val}",
        )

    # ==================== API 辅助方法 ====================

    async def _get_song_detail(self, hash_val: str) -> dict[str, Any]:
        """通过 /audio 接口获取歌曲详情"""
        url = f"{self.base_url}/audio?hash={hash_val}"
        try:
            async with self.session.get(url, headers=self.headers) as resp:
                if resp.status >= 400:
                    return {}
                data = await resp.json()
        except ClientError as e:
            raise ParseException(f"请求酷狗 API 服务失败: {e}") from e

        items = data.get("data", [])
        if isinstance(items, list) and items:
            return items[0]
        if isinstance(items, dict):
            return items
        return {}

    async def _get_krm_audio(self, album_audio_id: int) -> dict[str, Any]:
        """通过 /krm/audio 接口获取音频基本信息"""
        url = f"{self.base_url}/krm/audio?album_audio_id={album_audio_id}&fields=base,album_info"
        try:
            async with self.session.get(url, headers=self.headers) as resp:
                if resp.status < 400:
                    data = await resp.json()
                    res_list = data.get("data", [])
                    if isinstance(res_list, list) and res_list:
                        return res_list[0].get("base", {})
        except Exception:
            pass
        return {}

    async def _get_song_images(
        self, hash_val: str, album_id: int = 0, album_audio_id: int = 0
    ) -> tuple[str, str]:
        """通过 /images 接口获取歌曲专辑封面与歌手头像 (返回 (cover_url, avatar_url))"""
        url = f"{self.base_url}/images?hash={hash_val}&count=1"
        if album_id:
            url += f"&album_id={album_id}"
        if album_audio_id:
            url += f"&album_audio_id={album_audio_id}"

        async with self.session.get(url, headers=self.headers) as resp:
            if resp.status != 200:
                raise ParseException(f"获取酷狗图片接口失败: HTTP {resp.status}")
            data = await resp.json()

        data_list = data.get("data")
        if not data_list or not isinstance(data_list, list):
            raise ParseException("酷狗图片接口未返回有效数据")

        first_entry = data_list[0]
        cover = ""
        albums = first_entry.get("album") or []
        if albums:
            cover = albums[0].get("sizable_cover") or albums[0].get("cover") or ""

        avatar = ""
        authors = first_entry.get("author") or []
        if authors:
            avatar = authors[0].get("sizable_avatar") or authors[0].get("avatar") or ""

        if cover and "{size}" in cover:
            cover = cover.replace("{size}", "400")
        if avatar and "{size}" in avatar:
            avatar = avatar.replace("{size}", "400")

        return cover, avatar

    async def _query_song_url(
        self,
        hash_val: str,
        quality: str,
        album_id: int = 0,
        album_audio_id: int = 0,
    ) -> dict[str, Any]:
        """查询指定音质的播放 URL，不降级直接请求"""
        ts = int(time.time() * 1000)
        params_str = f"hash={hash_val}&quality={quality}&timestamp={ts}"
        if album_id:
            params_str += f"&album_id={album_id}"
        if album_audio_id:
            params_str += f"&album_audio_id={album_audio_id}"

        primary_url = f"{self.base_url}/song/url?{params_str}"
        return await self._fetch_url_from_endpoint(primary_url)

    async def _fetch_url_from_endpoint(self, endpoint: str) -> dict[str, Any]:
        """从指定接口请求并解析出播放直链与音频元数据"""
        async with self.session.get(endpoint, headers=self.headers) as resp:
            if resp.status != 200:
                err_text = await resp.text()
                raise ParseException(f"请求酷狗歌曲直链接口失败: HTTP {resp.status}, 详情: {err_text[:200]}")
            data = await resp.json()

        if data.get("status") != 1:
            err_msg = data.get("error") or data.get("errmsg") or f"状态码: {data.get('status')}"
            raise ParseException(f"酷狗 API 错误: {err_msg}")

        raw_urls = data.get("url")
        if not raw_urls:
            raise ParseException("酷狗 API 未返回播放链接")

        audio_url = raw_urls[0] if isinstance(raw_urls, list) else raw_urls
        file_size = int(data["fileSize"])
        extname = data.get("extName", "mp3")
        audio_hash = data.get("hash", "")

        return {
            "url": audio_url,
            "file_size": file_size,
            "extname": extname,
            "hash": audio_hash,
        }
