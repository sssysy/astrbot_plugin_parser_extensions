"""网易云音乐解析器 - 基于 NodeJS 网易云音乐 API"""

import asyncio
import base64
import json
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from re import Match
from typing import ClassVar

from PIL import Image, ImageDraw, ImageFont

from astrbot.api import logger

from ..config import PluginConfig
from ..cookie import CookieJar
from ..data import AudioContent, ImageContent, Platform, SendGroup
from ..download import Downloader
from ..exception import ParseException
from .base import BaseParser, handle

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

        logger.debug(f"[NCM] 初始化解析器 - base_url={self.base_url}, quality={self.quality}, cookies_len={len(self.cookiejar.cookies_str)}")

        self.headers.update({"Referer": "https://music.163.com"})
        self._sync_cookie_header()

    def _sync_cookie_header(self):
        """同步 cookie 到请求头，确保包含 os=pc"""
        cookie_str = self.cookiejar.cookies_str or ""
        logger.debug(f"[NCM] _sync_cookie_header - 原始 cookie 长度: {len(self.cookiejar.cookies_str)}")
        if "os=pc" not in cookie_str:
            cookie_str = f"{cookie_str}; os=pc".strip("; ")
            logger.debug("[NCM] _sync_cookie_header - 已追加 os=pc")
        self.headers["cookie"] = cookie_str
        logger.debug(f"[NCM] _sync_cookie_header - 最终 cookie 长度: {len(cookie_str)}")

    # ==================== 链接匹配 ====================

    @handle("163cn.tv", r"163cn\.tv/(?P<short_key>\w+)")
    async def _parse_short(self, searched: Match[str]):
        short_key = searched.group("short_key")
        short_url = f"https://163cn.tv/{short_key}"
        logger.debug(f"[NCM] _parse_short - 匹配到短链接, short_key={short_key}, 构建 URL={short_url}")
        return await self.parse_with_redirect(short_url)

    @handle("y.music.163.com", r"y\.music\.163\.com/m/song\?.*id=(?P<song_id>\d+)")
    @handle("music.163.com", r"music\.163\.com(?:/#)?/song\?.*id=(?P<song_id>\d+)")
    async def _parse_song(self, searched: Match[str]):
        song_id = searched.group("song_id")
        logger.debug(f"[NCM] _parse_song - 匹配到歌曲链接, song_id={song_id}, 原始文本片段={searched.group(0)[:100]}")
        return await self._process_song(song_id)

    @handle("playlist", r"music\.163\.com/#/playlist\?.*id=(?P<pl_id>\d+)")
    async def _parse_playlist(self, searched: Match[str]):
        pl_id = searched.group("pl_id")
        logger.debug(f"[NCM] _parse_playlist - 匹配到歌单链接, pl_id={pl_id}")
        raise ParseException("歌单解析暂不支持，请发送单曲链接")

    # ==================== 扫码登录 ====================

    async def login_with_qrcode(self) -> bytes:
        """获取登录二维码图片数据（返回 bytes）"""
        logger.debug("[NCM] login_with_qrcode - 开始扫码登录流程")
        ts = int(time.time() * 1000)
        logger.debug(f"[NCM] login_with_qrcode - timestamp={ts}")

        # Step 1: 获取 key
        key_url = f"{self.base_url}/login/qr/key?timestamp={ts}"
        logger.debug(f"[NCM] login_with_qrcode Step1 - 请求 key: {key_url}")
        async with self.session.get(key_url, headers=self.headers) as resp:
            logger.debug(f"[NCM] login_with_qrcode Step1 - 响应状态: HTTP {resp.status}")
            if resp.status >= 400:
                raise ParseException(f"获取二维码 key 失败: HTTP {resp.status}")
            key_data = await resp.json()
            logger.debug(f"[NCM] login_with_qrcode Step1 - 响应体: {json.dumps(key_data, ensure_ascii=False)}")
        unikey = key_data.get("data", {}).get("unikey")
        if not unikey:
            logger.debug(f"[NCM] login_with_qrcode Step1 - 未找到 unikey, data={key_data.get('data')}")
            raise ParseException("未能获取到二维码 key")
        self._qr_key = unikey
        logger.debug(f"[NCM] login_with_qrcode Step1 - 成功获取 unikey: {unikey}")

        # Step 2: 获取二维码
        qr_url = f"{self.base_url}/login/qr/create?key={unikey}&qrimg=true&timestamp={ts}"
        logger.debug(f"[NCM] login_with_qrcode Step2 - 请求二维码: {qr_url}")
        async with self.session.get(qr_url, headers=self.headers) as resp:
            logger.debug(f"[NCM] login_with_qrcode Step2 - 响应状态: HTTP {resp.status}")
            if resp.status >= 400:
                raise ParseException(f"获取二维码失败: HTTP {resp.status}")
            qr_data = await resp.json()
            logger.debug(f"[NCM] login_with_qrcode Step2 - qrimg 长度: {len(qr_data.get('data', {}).get('qrimg', ''))}")
        qr_img = qr_data.get("data", {}).get("qrimg", "")
        if not qr_img:
            raise ParseException("未能获取到二维码图片")

        logger.debug(f"[NCM] login_with_qrcode Step2 - 成功获取二维码图片")

        # 解码 Base64（可能带有 data:image 前缀）
        if qr_img.startswith("data:"):
            qr_img = qr_img.split(",", 1)[1]
            logger.debug("[NCM] login_with_qrcode - 去除 data:image 前缀后解码")
        result = base64.b64decode(qr_img)
        logger.debug(f"[NCM] login_with_qrcode - 解码完成, 图片大小: {len(result)} bytes")
        return result

    async def check_qr_state(self) -> AsyncGenerator[str, None]:
        """轮询二维码登录状态，yield 状态消息"""
        if not self._qr_key:
            logger.debug("[NCM] check_qr_state - qr_key 为空，无法轮询")
            yield "未找到二维码 key，请重新生成"
            return

        logger.debug(f"[NCM] check_qr_state - 开始轮询, qr_key={self._qr_key}")

        for idx in range(60):
            ts = int(time.time() * 1000)
            check_url = f"{self.base_url}/login/qr/check?key={self._qr_key}&timestamp={ts}"
            logger.debug(f"[NCM] check_qr_state 轮询#{idx + 1} - 请求: {check_url}")
            try:
                async with self.session.get(check_url, headers=self.headers) as resp:
                    logger.debug(f"[NCM] check_qr_state 轮询#{idx + 1} - 响应状态: HTTP {resp.status}")
                    if resp.status == 502:
                        logger.debug(f"[NCM] check_qr_state 轮询#{idx + 1} - 收到502, 带noCookie重试")
                        # 502 需带 noCookie 重试
                        retry_url = (
                            f"{self.base_url}/login/qr/check"
                            f"?key={self._qr_key}&timestamp={ts}&noCookie=true"
                        )
                        async with self.session.get(retry_url, headers=self.headers) as resp2:
                            logger.debug(f"[NCM] check_qr_state 轮询#{idx + 1} - noCookie重试响应: HTTP {resp2.status}")
                            if resp2.status >= 400:
                                await asyncio.sleep(3)
                                continue
                            check_data = await resp2.json()
                    elif resp.status >= 400:
                        await asyncio.sleep(3)
                        continue
                    else:
                        check_data = await resp.json()
            except Exception as e:
                logger.debug(f"[NCM] check_qr_state 轮询#{idx + 1} - 异常: {e}")
                await asyncio.sleep(3)
                continue

            code = check_data.get("code")
            logger.debug(f"[NCM] check_qr_state 轮询#{idx + 1} - 响应 code={code}, message={check_data.get('message', '')}")
            if code == 800:
                logger.debug("[NCM] check_qr_state - 二维码已过期 (code=800)")
                yield "二维码已过期，请重新生成"
                return
            elif code == 801:
                logger.debug("[NCM] check_qr_state - 等待扫码 (code=801)")
                await asyncio.sleep(3)
                continue
            elif code == 802:
                logger.debug("[NCM] check_qr_state - 已扫码待确认 (code=802)")
                yield "已扫码，请在手机上确认授权"
                await asyncio.sleep(3)
                continue
            elif code == 803:
                logger.debug("[NCM] check_qr_state - 登录成功 (code=803)")
                cookie_str = check_data.get("cookie", "")
                logger.debug(f"[NCM] check_qr_state - 收到cookie长度: {len(cookie_str)}")
                if cookie_str:
                    self._save_cookies(cookie_str)
                yield "网易云音乐登录成功"
                return
            else:
                logger.debug(f"[NCM] check_qr_state 轮询#{idx + 1} - 未知code={code}, 继续等待")
                await asyncio.sleep(3)
                continue
        else:
            logger.debug("[NCM] check_qr_state - 轮询超时(60次)")
            yield "登录超时，请重新生成二维码"

    def _save_cookies(self, cookie_str: str):
        """保存 cookies 到本地文件并更新请求头"""
        logger.debug(f"[NCM] _save_cookies - 开始保存cookie, 长度: {len(cookie_str)}")
        self.cookiejar._load_from_cookies_str(cookie_str)
        self.cookiejar.save_to_file()
        self.cookiejar.cookies_str = self.cookiejar.clean_cookies_str(cookie_str)
        logger.debug(f"[NCM] _save_cookies - 清理后cookie长度: {len(self.cookiejar.cookies_str)}")
        self._sync_cookie_header()
        self.mycfg.cookies = self.cookiejar.cookies_str
        try:
            self.cfg.save_config()
            logger.debug("[NCM] _save_cookies - 配置已保存")
        except Exception as e:
            logger.debug(f"[NCM] _save_cookies - 保存配置异常: {e}")

    # ==================== 歌曲处理核心 ====================

    async def _process_song(self, song_id: str):
        """处理单曲解析的完整流程"""
        logger.debug(f"[NCM] _process_song - 开始处理 song_id={song_id}")

        # 1. 获取歌曲详情
        detail = await self._get_song_detail(song_id)
        if not detail:
            logger.debug(f"[NCM] _process_song - song_id={song_id} 未找到歌曲详情")
            raise ParseException("未找到该歌曲")
        logger.debug(f"[NCM] _process_song - 获取详情成功, keys={list(detail.keys())}")

        song_name = detail.get("name", "未知歌曲")
        song_dt = detail.get("dt", 0)
        duration_sec = song_dt // 1000
        logger.debug(f"[NCM] _process_song - 歌曲名={song_name}, dt={song_dt}ms, 时长={duration_sec}s")

        ar_list = detail.get("ar", [])
        artist_name = " / ".join(ar.get("name", "") for ar in ar_list)
        logger.debug(f"[NCM] _process_song - 歌手列表: {[ar.get('name') for ar in ar_list]}")

        al = detail.get("al", {})
        cover_url = al.get("picUrl", "")
        if cover_url:
            cover_url += "?param=640y640"
        logger.debug(f"[NCM] _process_song - 封面URL: {cover_url[:80]}...")

        privilege = detail.get("privilege", {})
        max_br_level = privilege.get("maxBrLevel", "")
        logger.debug(f"[NCM] _process_song - privilege={privilege}, max_br_level={max_br_level}")

        # 2. 确定下载音质
        target_level = self._resolve_quality(max_br_level)
        logger.debug(f"[NCM] _process_song - 音质解析结果: target_level={target_level}")

        # 3. 获取歌曲 URL
        url_info = await self._get_song_url(song_id, target_level)
        audio_url = url_info.get("url", "")
        file_size = url_info.get("size", 0)
        audio_type = url_info.get("type", "mp3")
        logger.debug(f"[NCM] _process_song - 首次获取URL: url={'有' if audio_url else '无'}, size={file_size}, level={target_level}, type={audio_type}")

        # 降级重试
        if not audio_url:
            logger.debug(f"[NCM] _process_song - 音质{target_level}无可用URL, 开始降级重试")
            for fb in ("exhigh", "higher", "standard"):
                if fb == target_level:
                    continue
                url_info = await self._get_song_url(song_id, fb)
                audio_url = url_info.get("url", "")
                logger.debug(f"[NCM] _process_song - 降级尝试 level={fb}: url={'有' if audio_url else '无'}")
                if audio_url:
                    target_level = fb
                    file_size = url_info.get("size", 0)
                    audio_type = url_info.get("type", "mp3")
                    break

        if not audio_url:
            logger.debug(f"[NCM] _process_song - 所有音质均无可用URL")
            raise ParseException("该歌曲暂无可用播放地址")

        logger.debug(f"[NCM] _process_song - 最终: level={target_level}, size={file_size}, type={audio_type}")

        # 4. 生成预览图
        preview_path = await self._generate_preview(
            song_name=song_name,
            artist_name=artist_name,
            cover_url=cover_url,
            duration_sec=duration_sec,
            file_size=file_size,
            quality=target_level,
        )
        logger.debug(f"[NCM] _process_song - 预览图: {'成功' if preview_path else '失败'}")

        # 5. 下载音频
        ext = f".{audio_type}"
        audio_name = f"{song_name} - {artist_name}{ext}"
        audio_task = self.downloader.download_audio(
            audio_url,
            audio_name=audio_name,
            headers=self.headers,
            proxy=self.proxy,
        )
        logger.debug(f"[NCM] _process_song - 音频下载任务已创建: {audio_name}")

        # 6. 构建结果
        author = self.create_author(artist_name)
        audio_content = AudioContent(audio_task, duration_sec)

        send_groups = []
        if preview_path and preview_path.exists():
            send_groups.append(SendGroup(contents=[ImageContent(preview_path)], force_merge=False))
        send_groups.append(SendGroup(contents=[audio_content], force_merge=False, render_card=False))

        logger.debug(f"[NCM] _process_song - 构建结果完成, send_groups数量: {len(send_groups)}")
        return self.result(
            title=f"{song_name} - {artist_name}",
            text=f"音质: {target_level}",
            author=author,
            contents=[audio_content],
            send_groups=send_groups,
            url=f"https://music.163.com/#/song?id={song_id}",
        )

    async def _get_song_detail(self, song_id: str) -> dict | None:
        """获取歌曲详情"""
        url = f"{self.base_url}/song/detail?ids={song_id}"
        logger.debug(f"[NCM] _get_song_detail - 请求: {url}")
        async with self.session.get(url, headers=self.headers) as resp:
            logger.debug(f"[NCM] _get_song_detail - HTTP状态: {resp.status}")
            if resp.status >= 400:
                raise ParseException(f"获取歌曲详情失败: HTTP {resp.status}")
            data = await resp.json()
            logger.debug(f"[NCM] _get_song_detail - 响应code={data.get('code')}, songs数量={len(data.get('songs', []))}")
        songs = data.get("songs", [])
        return songs[0] if songs else None

    async def _get_song_url(self, song_id: str, level: str) -> dict:
        """获取歌曲播放 URL"""
        url = f"{self.base_url}/song/url/v1?id={song_id}&level={level}"
        logger.debug(f"[NCM] _get_song_url - 请求: {url}")
        async with self.session.get(url, headers=self.headers) as resp:
            logger.debug(f"[NCM] _get_song_url - HTTP状态: {resp.status}")
            if resp.status >= 400:
                raise ParseException(f"获取歌曲 URL 失败: HTTP {resp.status}")
            data = await resp.json()
            logger.debug(f"[NCM] _get_song_url - 响应code={data.get('code')}, data数量={len(data.get('data', []))}")
        results = data.get("data", [])
        item = results[0] if results else {}
        logger.debug(f"[NCM] _get_song_url - 结果: url={'有' if item.get('url') else '无'}, size={item.get('size', 0)}, type={item.get('type', '')}, br={item.get('br', 0)}")
        return item

    def _resolve_quality(self, max_br_level: str) -> str:
        """根据配置和歌曲支持的最高音质，确定实际下载音质"""
        target = self.quality
        logger.debug(f"[NCM] _resolve_quality - 配置音质={target}, 歌曲最高音质={max_br_level}")
        if not max_br_level:
            logger.debug(f"[NCM] _resolve_quality - max_br_level为空, 使用配置音质={target}")
            return target
        target_rank = QUALITY_RANK.get(target, 2)
        max_rank = QUALITY_RANK.get(max_br_level, 2)
        logger.debug(f"[NCM] _resolve_quality - target_rank={target_rank}, max_rank={max_rank}")
        if target_rank <= max_rank:
            return target
        logger.debug(f"[NCM] _resolve_quality - 配置音质高于最高可用, 降级到={max_br_level}")
        return max_br_level

    # ==================== 预览图生成 ====================

    async def _generate_preview(
        self,
        song_name: str,
        artist_name: str,
        cover_url: str,
        duration_sec: int,
        file_size: int,
        quality: str,
    ) -> Path | None:
        """生成标准化的歌曲预览图"""
        logger.debug(f"[NCM] _generate_preview - 开始, 歌曲={song_name}, 封面URL={'有' if cover_url else '无'}")
        try:
            cover_path = None
            if cover_url:
                cover_path = await self.downloader.download_img(
                    cover_url, headers=self.headers, proxy=self.proxy
                )
                logger.debug(f"[NCM] _generate_preview - 封面下载: {'成功' if cover_path and cover_path.exists() else '失败'}")

            font_path = (
                Path(__file__).parent.parent / "resources" / "HYSongYunLangHeiW-1.ttf"
            )
            if not font_path.exists():
                logger.debug(f"[NCM] _generate_preview - 字体文件不存在: {font_path}")
                font_path = None

            logger.debug(f"[NCM] _generate_preview - 开始异步绘制预览图")
            return await asyncio.to_thread(
                self._draw_preview,
                song_name,
                artist_name,
                cover_path,
                duration_sec,
                file_size,
                quality,
                font_path,
            )
        except Exception as e:
            logger.warning(f"生成预览图失败: {e}")
            logger.debug(f"[NCM] _generate_preview - 异常堆栈: {e.__class__.__name__}: {e}")
            return None

    def _draw_preview(
        self,
        song_name: str,
        artist_name: str,
        cover_path: Path | None,
        duration_sec: int,
        file_size: int,
        quality: str,
        font_path: Path | None,
    ) -> Path:
        """使用 PIL 绘制预览图"""
        logger.debug(f"[NCM] _draw_preview - 开始绘制, 歌曲={song_name}, 封面路径={cover_path}")
        CARD_WIDTH = 720
        PADDING = 30
        COVER_SIZE = 400
        BG_COLOR = (255, 255, 255)
        TITLE_COLOR = (51, 51, 51)
        SUB_COLOR = (136, 136, 136)
        ACCENT_COLOR = (0, 122, 255)

        # 加载封面
        cover_img = None
        if cover_path and cover_path.exists():
            try:
                cover_img = Image.open(cover_path).convert("RGB")
                cover_img = cover_img.resize(
                    (COVER_SIZE, COVER_SIZE), Image.Resampling.LANCZOS
                )
                logger.debug(f"[NCM] _draw_preview - 封面加载成功, 尺寸={cover_img.size}")
            except Exception as e:
                logger.debug(f"[NCM] _draw_preview - 封面加载失败: {e}")
                cover_img = None

        if cover_img is None:
            cover_img = Image.new("RGB", (COVER_SIZE, COVER_SIZE), (230, 230, 230))
            logger.debug("[NCM] _draw_preview - 使用灰色占位封面")

        # 加载字体
        if font_path:
            font_title = ImageFont.truetype(str(font_path), 32)
            font_artist = ImageFont.truetype(str(font_path), 24)
            font_meta = ImageFont.truetype(str(font_path), 20)
            logger.debug(f"[NCM] _draw_preview - 使用自定义字体: {font_path}")
        else:
            font_title = ImageFont.load_default()
            font_artist = ImageFont.load_default()
            font_meta = ImageFont.load_default()
            logger.debug("[NCM] _draw_preview - 使用默认字体")

        # 文本准备
        title_text = self._truncate_text(song_name, font_title, CARD_WIDTH - 2 * PADDING)
        artist_text = artist_name
        meta_text = self._format_duration(duration_sec)
        if file_size:
            meta_text += f" | {self._format_size(file_size)}"
        meta_text += f" | {quality}"
        logger.debug(f"[NCM] _draw_preview - 标题={title_text}, 歌手={artist_text}, 元数据={meta_text}")

        # 计算尺寸
        GAP = 12

        def _text_size(font, text: str) -> tuple[int, int]:
            bbox = font.getbbox(text)
            return bbox[2] - bbox[0], bbox[3] - bbox[1]

        title_w, title_h = _text_size(font_title, title_text)
        artist_w, artist_h = _text_size(font_artist, artist_text)
        meta_w, meta_h = _text_size(font_meta, meta_text)
        logger.debug(f"[NCM] _draw_preview - 文本尺寸: title={title_w}x{title_h}, artist={artist_w}x{artist_h}, meta={meta_w}x{meta_h}")

        total_height = (
            PADDING + COVER_SIZE + GAP + title_h + GAP
            + artist_h + GAP + meta_h + PADDING
        )
        logger.debug(f"[NCM] _draw_preview - 画布尺寸: {CARD_WIDTH}x{total_height}")

        # 创建画布
        image = Image.new("RGB", (CARD_WIDTH, total_height), BG_COLOR)
        draw = ImageDraw.Draw(image)

        y = PADDING

        # 封面居中
        cover_x = (CARD_WIDTH - COVER_SIZE) // 2
        image.paste(cover_img, (cover_x, y))
        y += COVER_SIZE + GAP

        # 歌名居中
        draw.text(
            ((CARD_WIDTH - title_w) // 2, y),
            title_text,
            fill=TITLE_COLOR,
            font=font_title,
        )
        y += title_h + GAP

        # 歌手居中
        draw.text(
            ((CARD_WIDTH - artist_w) // 2, y),
            artist_text,
            fill=SUB_COLOR,
            font=font_artist,
        )
        y += artist_h + GAP

        # 元数据居中
        draw.text(
            ((CARD_WIDTH - meta_w) // 2, y),
            meta_text,
            fill=ACCENT_COLOR,
            font=font_meta,
        )

        cache_path = (
            self.cfg.cache_dir / f"ncm_preview_{hash(song_name) & 0xFFFFFFFF}.png"
        )
        image.save(cache_path, "PNG")
        logger.debug(f"[NCM] _draw_preview - 预览图已保存: {cache_path}")
        return cache_path

    @staticmethod
    def _truncate_text(text: str, font, max_width: int) -> str:
        """截断文本使其不超过指定宽度"""
        try:
            actual_w = font.getlength(text)
        except AttributeError:
            bbox = font.getbbox(text)
            actual_w = bbox[2] - bbox[0]
        logger.debug(f"[NCM] _truncate_text - 原始文本='{text}', 宽度={actual_w:.0f}, 最大={max_width}")
        if actual_w <= max_width:
            return text

        for i in range(len(text) - 1, 0, -1):
            truncated = text[:i] + "..."
            try:
                w = font.getlength(truncated)
            except AttributeError:
                bbox = font.getbbox(truncated)
                w = bbox[2] - bbox[0]
            if w <= max_width:
                logger.debug(f"[NCM] _truncate_text - 截断为='{truncated}', 宽度={w:.0f}")
                return truncated
        logger.debug(f"[NCM] _truncate_text - 最终截断为'...'")
        return "..."

    @staticmethod
    def _format_duration(seconds: int) -> str:
        """格式化时长 mm:ss"""
        m, s = divmod(seconds, 60)
        return f"{m}:{s:02d}"

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        """格式化文件大小"""
        if size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f}KB"
        return f"{size_bytes / 1024 / 1024:.1f}MB"