"""网易云音乐解析器 - 基于 NodeJS 网易云音乐 API"""

import asyncio
import base64
import time
from collections.abc import AsyncGenerator
from re import Match
from typing import ClassVar

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

# 音质等级映射（用于比较音质高低）
QUALITY_RANK = {
    "standard": 0,
    "higher": 1,
    "exhigh": 2,
    "lossless": 3,
    "hires": 4,
    "jyeffect": 5,
    "sky": 6,
    "dolby": 7,
    "jymaster": 8,
}


class NCMParser(BaseParser):
    """网易云音乐解析器"""

    platform: ClassVar[Platform] = Platform(name="ncm", display_name="网易云")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.ncm
        self.base_url = (self.mycfg.base_url or "http://localhost:3000").rstrip("/")
        self.quality = self.mycfg.quality or "exhigh"
        self.cookiejar = CookieJar(config, self.mycfg, domain="music.163.com")
        self._qr_key: str | None = None

        self.headers.update({"Referer": "https://music.163.com"})
        self._sync_cookie_header()

    def _sync_cookie_header(self) -> None:
        """同步 cookie 到请求头，确保包含 os=pc"""
        cookie_str = self.cookiejar.cookies_str or ""
        if cookie_str and "os=pc" not in cookie_str:
            cookie_str = f"{cookie_str}; os=pc".strip("; ")
        self.headers["cookie"] = cookie_str

    # ==================== 链接匹配 ====================

    @handle("163cn.tv", r"163cn\.tv/(?P<short_key>\w+)")
    async def _parse_short(self, searched: Match[str]):
        short_key = searched.group("short_key")
        return await self.parse_with_redirect(f"https://163cn.tv/{short_key}")

    @handle("y.music.163.com", r"y\.music\.163\.com/m/song\?.*id=(?P<song_id>\d+)")
    @handle("music.163.com", r"music\.163\.com(?:/#)?/song\?.*id=(?P<song_id>\d+)")
    async def _parse_song(self, searched: Match[str]):
        song_id = searched.group("song_id")
        return await self._process_song(song_id)

    @handle("playlist", r"music\.163\.com(?:/#)?/playlist\?.*id=(?P<pl_id>\d+)")
    async def _parse_playlist(self, searched: Match[str]):
        raise ParseException("歌单解析暂不支持，请发送单曲链接")

    @handle("music.126.net", r"https?://[^/]*music\.126\.net/.*\.mp3(?:\?.*)?$")
    async def _parse_direct_mp3(self, searched: Match[str]):
        url = searched.group(0)
        audio = self.create_audio_content(url)
        return self.result(
            title="网易云音乐",
            text="直链音频",
            contents=[audio],
            url=url,
        )

    @handle(
        "music.163.com/song/media/outer/url",
        r"(https?://music\.163\.com/song/media/outer/url\?[^>\s]+)",
    )
    async def _parse_private_outer(self, searched: Match[str]):
        private_url = searched.group(0)
        audio = self.create_audio_content(private_url)
        return self.result(
            title="网易云音乐（私人直链）",
            text="直链音频",
            contents=[audio],
            url=private_url,
        )

    # ==================== 扫码登录 ====================

    async def login_with_qrcode(self) -> bytes:
        """获取登录二维码图片数据（返回 bytes）"""
        ts = int(time.time() * 1000)

        # Step 1: 获取 key
        key_url = f"{self.base_url}/login/qr/key?timestamp={ts}"
        try:
            async with self.session.get(key_url, headers=self.headers) as resp:
                if resp.status >= 400:
                    raise ParseException(f"获取二维码 key 失败: HTTP {resp.status}")
                key_data = await resp.json()
        except ClientError as e:
            raise ParseException(f"连接网易云 API 服务失败: {e}") from e

        unikey = key_data.get("data", {}).get("unikey")
        if not unikey:
            raise ParseException("未能获取到二维码 key")
        self._qr_key = unikey

        # Step 2: 获取二维码
        qr_url = f"{self.base_url}/login/qr/create?key={unikey}&qrimg=true&timestamp={ts}"
        try:
            async with self.session.get(qr_url, headers=self.headers) as resp:
                if resp.status >= 400:
                    raise ParseException(f"获取二维码失败: HTTP {resp.status}")
                qr_data = await resp.json()
        except ClientError as e:
            raise ParseException(f"连接网易云 API 服务失败: {e}") from e

        qr_img = qr_data.get("data", {}).get("qrimg", "")
        if not qr_img:
            raise ParseException("未能获取到二维码图片")

        if qr_img.startswith("data:"):
            qr_img = qr_img.split(",", 1)[1]
        return base64.b64decode(qr_img)

    async def check_qr_state(self) -> AsyncGenerator[str, None]:
        """轮询二维码登录状态，yield 状态消息"""
        if not self._qr_key:
            yield "未找到二维码 key，请重新生成"
            return

        for _ in range(60):
            ts = int(time.time() * 1000)
            check_url = f"{self.base_url}/login/qr/check?key={self._qr_key}&timestamp={ts}"
            try:
                async with self.session.get(check_url, headers=self.headers) as resp:
                    if resp.status == 502:
                        retry_url = f"{check_url}&noCookie=true"
                        async with self.session.get(retry_url, headers=self.headers) as resp2:
                            if resp2.status >= 400:
                                await asyncio.sleep(3)
                                continue
                            check_data = await resp2.json()
                    elif resp.status >= 400:
                        await asyncio.sleep(3)
                        continue
                    else:
                        check_data = await resp.json()
            except Exception:
                await asyncio.sleep(3)
                continue

            code = check_data.get("code")
            if code == 800:
                yield "二维码已过期，请重新生成"
                return
            elif code == 801:
                await asyncio.sleep(3)
                continue
            elif code == 802:
                yield "已扫码，请在手机上确认授权"
                await asyncio.sleep(3)
                continue
            elif code == 803:
                cookie_str = check_data.get("cookie", "")
                if cookie_str:
                    self._save_cookies(cookie_str)
                yield "网易云音乐登录成功"
                return
            else:
                await asyncio.sleep(3)
                continue

        yield "登录超时，请重新生成二维码"

    def _save_cookies(self, cookie_str: str) -> None:
        """保存 cookies 到本地文件并更新请求头"""
        self.cookiejar._load_from_cookies_str(cookie_str)
        self.cookiejar.save_to_file()
        self.cookiejar.cookies_str = self.cookiejar.clean_cookies_str(cookie_str)
        self._sync_cookie_header()
        self.mycfg.cookies = self.cookiejar.cookies_str
        try:
            self.cfg.save_config()
        except Exception:
            pass

    # ==================== 歌曲处理核心 ====================

    async def _process_song(self, song_id: str):
        """处理单曲解析"""
        # 1. 获取歌曲详情
        detail = await self._get_song_detail(song_id)

        song_name = detail.get("name") or "未知歌曲"
        aliases = detail.get("alia", []) or detail.get("alias", [])
        sub_title = f" ({aliases[0]})" if aliases else ""
        title = f"{song_name}{sub_title}"

        song_dt = detail.get("dt", 0)
        duration_sec = song_dt // 1000

        ar_list = detail.get("ar", []) or detail.get("artists", [])
        artist_name = " & ".join(ar.get("name", "") for ar in ar_list if ar.get("name")) or "未知歌手"
        author_avatar = ar_list[0].get("img1v1Url") or ar_list[0].get("picUrl") if ar_list else None

        album = detail.get("al", {}) or detail.get("album", {})
        album_name = album.get("name", "")
        cover_url = album.get("picUrl", "")
        if cover_url and "?param=" not in cover_url:
            cover_url += "?param=640y640"

        privilege = detail.get("privilege", {})
        max_br_level = privilege.get("maxBrLevel", "")

        # 2. 确定下载音质与播放地址
        target_level = self._resolve_quality(max_br_level)
        url_info = await self._get_song_url(song_id, target_level)
        audio_url = url_info.get("url", "")
        file_size = url_info.get("size", 0)
        audio_type = url_info.get("type") or "mp3"

        # 音质降级重试
        if not audio_url:
            for fallback_level in ("exhigh", "higher", "standard"):
                if fallback_level == target_level:
                    continue
                url_info = await self._get_song_url(song_id, fallback_level)
                if url_info.get("url"):
                    target_level = fallback_level
                    audio_url = url_info["url"]
                    file_size = url_info.get("size", 0)
                    audio_type = url_info.get("type") or "mp3"
                    break

        if not audio_url:
            raise ParseException("该歌曲暂无可用播放地址（可能需要VIP或无版权）")

        # 3. 创建音频与封面内容
        audio_name = f"{song_name} - {artist_name}.{audio_type}"
        audio_task = self.downloader.download_audio(
            audio_url,
            audio_name=audio_name,
            headers=self.headers,
            proxy=self.proxy,
        )
        audio_content = AudioContent(audio_task, duration=duration_sec)

        author = self.create_author(artist_name, avatar_url=author_avatar)

        # 放入封面 ImageContent，以便插件 render_card 绘制卡片
        card_contents: list[MediaContent] = []
        if cover_url:
            cover_task = self.downloader.download_img(
                cover_url, headers=self.headers, proxy=self.proxy
            )
            card_contents.append(ImageContent(cover_task))

        # 4. 元数据与发送策略构建
        minutes, seconds = divmod(duration_sec, 60)
        meta_parts = [f"音质: {target_level}", f"时长: {minutes}:{seconds:02d}"]
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
            url=f"https://music.163.com/#/song?id={song_id}",
        )

    async def _get_song_detail(self, song_id: str) -> dict:
        """获取歌曲详情"""
        url = f"{self.base_url}/song/detail?ids={song_id}"
        try:
            async with self.session.get(url, headers=self.headers) as resp:
                if resp.status >= 400:
                    raise ParseException(f"获取歌曲详情失败: HTTP {resp.status}")
                data = await resp.json()
        except ClientError as e:
            raise ParseException(f"请求网易云 API 失败: {e}") from e

        songs = data.get("songs", [])
        if not songs:
            raise ParseException("未找到该歌曲详情")

        song = songs[0]
        privileges = data.get("privileges", [])
        if privileges and "privilege" not in song:
            song["privilege"] = privileges[0]
        return song

    async def _get_song_url(self, song_id: str, level: str) -> dict:
        """获取歌曲播放 URL"""
        url = f"{self.base_url}/song/url/v1?id={song_id}&level={level}"
        try:
            async with self.session.get(url, headers=self.headers) as resp:
                if resp.status >= 400:
                    raise ParseException(f"获取歌曲链接失败: HTTP {resp.status}")
                data = await resp.json()
        except ClientError as e:
            raise ParseException(f"请求网易云 API 失败: {e}") from e

        results = data.get("data", [])
        return results[0] if results else {}

    def _resolve_quality(self, max_br_level: str) -> str:
        """根据配置和歌曲支持的最高音质确定目标音质"""
        target = self.quality
        if not max_br_level:
            return target
        target_rank = QUALITY_RANK.get(target, 2)
        max_rank = QUALITY_RANK.get(max_br_level, 2)
        return target if target_rank <= max_rank else max_br_level