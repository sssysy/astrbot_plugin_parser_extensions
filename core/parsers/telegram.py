from __future__ import annotations

import asyncio
import io
import subprocess
import sys
from pathlib import Path
from re import Match
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import urlparse

import qrcode
from aiohttp import ClientError
from astrbot.api import logger

from data.plugins.astrbot_plugin_parser.core.data import (
    AudioContent,
    Author,
    DynamicContent,
    FileContent,
    ImageContent,
    MediaContent,
    ParseResult,
    Platform,
    SendGroup,
    TextContent,
    VideoContent,
)
from data.plugins.astrbot_plugin_parser.core.download import Downloader
from data.plugins.astrbot_plugin_parser.core.exception import ParseException
from data.plugins.astrbot_plugin_parser.core.parsers.base import BaseParser, handle

from ..config import PluginConfig


if TYPE_CHECKING:
    from telethon import TelegramClient
    from telethon.tl.custom import Message
    from telethon.tl.custom.qrlogin import QRLogin


def import_telethon() -> bool:
    """懒导入 telethon 库,失败则尝试从镜像源自动安装。"""
    if _try_import():
        logger.info("[parserplugin-telegram] telethon 库依赖加载完毕")
        return True
    logger.warning("[parserplugin-telegram] 依赖缺失,开始尝试自动安装")
    mirrors = [
        ("清华源", "https://pypi.tuna.tsinghua.edu.cn/simple"),
        ("官方 PyPI", "https://pypi.org/simple"),
    ]
    for name, index_url in mirrors:
        try:
            logger.info(f"[parserplugin-telegram] 正在从 {name} 安装 telethon ...")
            subprocess.check_call(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--no-input",
                    "telethon",
                    "-i",
                    index_url,
                ]
            )
        except subprocess.CalledProcessError as e:
            logger.warning(f"[parserplugin-telegram] 从 {name} 安装 telethon 失败: {e}")
            continue
        if _try_import():
            logger.info(f"[parserplugin-telegram] telethon 库依赖加载完毕")
            return True
        logger.warning(
            f"[parserplugin-telegram] 从 {name} 安装后仍导入失败,请尝试手动安装 telethon"
        )
    logger.error("[parserplugin-telegram] 所有镜像源安装均失败")
    return False


def _try_import() -> bool:
    try:
        import telethon  # type: ignore

        return True
    except ImportError:
        return False


def _make_qr_png(url: str) -> bytes:
    """生成二维码"""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,  # type: ignore
        box_size=10,
        border=2,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")  # type: ignore
    return buf.getvalue()


class utils:
    @staticmethod
    def parse_proxy(proxy: str | None):
        """将 HTTP 代理 URL 转为 Telethon 的 (scheme, host, port) 元组格式。"""
        if not proxy:
            return None
        parsed = urlparse(proxy)
        if not parsed.hostname or not parsed.port:
            return None
        return (parsed.scheme, parsed.hostname, parsed.port)


class TelegramLogin:
    """Telegram 登录流程管理:QR 扫码 + 2FA 分步完成。"""

    def __init__(self, parser: "TelegramParser"):
        self.parser = parser
        self._qr: "QRLogin | None" = None
        self._awaiting_2fa: bool = False

    @property
    def client(self) -> "TelegramClient":
        return self.parser.tgclient

    async def _ensure_connected(self) -> None:
        client = self.client
        if not client.is_connected():
            await client.connect()

    async def is_logged_in(self) -> bool:
        try:
            await self._ensure_connected()
            return bool(await self.client.get_me())
        except Exception:
            return False

    async def start_qr_login(self) -> bytes:
        """生成二维码 PNG 字节。若已登录则登出旧 session 后重新登录(覆盖)。"""
        await self._ensure_connected()
        if await self.is_logged_in():
            logger.info("[parserplugin-telegram] 已登录,正在登出旧 session 以覆盖")
            try:
                await self.client.log_out()
            except Exception as e:
                logger.warning(f"[parserplugin-telegram] 登出旧 session 失败,继续尝试登录: {e}")
            await self._ensure_connected()
        self._qr = await self.client.qr_login()
        self._awaiting_2fa = False
        return _make_qr_png(self._qr.url)

    async def wait_qr_login(self):
        """阻塞等待扫码结果,以 async generator 形式 yield 提示文本。"""
        from telethon.errors import SessionPasswordNeededError

        if self._qr is None:
            yield "请先使用 tglogin 生成二维码"
            return
        try:
            await self._qr.wait()
        except asyncio.TimeoutError:
            self._qr = None
            self._awaiting_2fa = False
            yield "二维码已过期,请重新使用 tglogin 生成"
            return
        except SessionPasswordNeededError:
            # 保留状态,等待 2FA 指令
            self._awaiting_2fa = True
            yield "检测到两步验证(2FA),请使用指令: tglogin 2fa <密码> 完成登录"
            return
        except Exception as e:
            self._qr = None
            self._awaiting_2fa = False
            yield f"登录失败: {e}"
            return
        me = await self.client.get_me()
        self._qr = None
        self._awaiting_2fa = False
        username = getattr(me, "username", None) or getattr(me, "id", "")
        yield f"登录成功 (@{username})"

    async def complete_2fa(self, password: str) -> str:
        """用 2FA 密码完成登录。"""
        if not self._awaiting_2fa:
            return "当前无需 2FA 验证,请先使用 tglogin 生成二维码并扫码"
        try:
            await self.client.sign_in(password=password)
        except Exception as e:
            # 密码错误等,保留 _awaiting_2fa 让用户可重试
            return f"2FA 验证失败: {e}"
        me = await self.client.get_me()
        self._awaiting_2fa = False
        self._qr = None
        username = getattr(me, "username", None) or getattr(me, "id", "")
        return f"登录成功 (@{username})"


class TelegramParser(BaseParser):

    platform: ClassVar[Platform] = Platform(name="telegram", display_name="Telegram")

    ALBUM_WINDOW: ClassVar[int] = 10
    """相册窗口大小"""

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.telegram
        self._tg_client: "TelegramClient | None" = None
        self.login: "TelegramLogin | None" = None

        if not import_telethon():
            logger.warning(
                "[parserplugin-telegram] 依赖不可用,已从配置禁用 telegram 解析器并保存"
            )
            try:
                config.parser.telegram.enable = False
                config.save_config()
            except Exception as e:
                logger.error(
                    f"[parserplugin-telegram] 禁用 telegram 配置失败: {e}。请手动禁用 telegram 解析器"
                )
            TelegramParser._key_patterns = []
            return

        self.login = TelegramLogin(self)

    # ---------- client 生命周期 ----------
    def _resolve_session(self) -> str:
        """返回默认 session 文件路径 (cookie_dir/telegram_session)"""
        return str(self.cfg.cookie_dir / "telegram_session")

    def _get_client(self) -> "TelegramClient":
        """懒初始化 TelegramClient"""
        if self._tg_client is None:
            from telethon import TelegramClient

            api_id = self.mycfg.api_id
            api_hash = self.mycfg.api_hash
            proxy = utils.parse_proxy(self.proxy) if self.proxy else None
            self._tg_client = TelegramClient(
                session=self._resolve_session(),
                api_id=api_id, # type: ignore
                api_hash=api_hash, # type: ignore
                proxy=proxy,  # type: ignore
            )
            logger.info("[parserplugin-telegram] 初始化 telegram client 成功")
        return self._tg_client

    @property
    def tgclient(self) -> "TelegramClient":
        return self._get_client()

    async def reset_client(self) -> None:
        """配置变更时重建 client"""
        if self._tg_client is not None:
            try:
                await self._tg_client.disconnect()  # type: ignore[union-attr]
            except Exception:
                pass
        self._tg_client = None
        if self.login is not None:
            self.login._qr = None
            self.login._awaiting_2fa = False

    async def close_session(self) -> None:
        await super().close_session()
        if self._tg_client is not None:
            try:
                await self._tg_client.disconnect()  # type: ignore[union-attr]
            except Exception:
                pass

    async def _ensure_logged_in(self) -> "TelegramClient":
        """确保已连接且已登录,未登录抛 ParseException"""
        client = self._get_client()
        if not client.is_connected():
            await client.connect()
        try:
            me = await client.get_me()
        except Exception as e:
            raise ParseException(
                f"Telegram 未登录或 session 已失效,请使用 tglogin 登录: {e}"
            )
        if not me:
            raise ParseException("Telegram 未登录,请使用 tglogin 登录")
        return client

    # ---------- URL 处理器 ----------
    # 群组
    @handle("t.me/c/", r"t\.me/c/(?P<channel_id>\d+)/(?P<msg_id>\d+)")
    async def _parse_tg_private(self, searched: Match[str]) -> ParseResult:
        channel_id = int(searched.group("channel_id"))
        msg_id = int(searched.group("msg_id"))
        entity = int(f"-100{channel_id}")  # marked peer id
        url = f"https://t.me/c/{channel_id}/{msg_id}"
        return await self._parse_message(entity, msg_id, url)

    # 频道
    @handle("t.me", r"t\.me/(?P<channel>[^/]+)/(?P<msg_id>\d+)")
    async def _parse_tg_public(self, searched: Match[str]) -> ParseResult:
        channel = searched.group("channel")
        if channel == "c":  # 保险检查
            raise ParseException("无法解析该 Telegram 链接")
        msg_id = int(searched.group("msg_id"))
        url = f"https://t.me/{channel}/{msg_id}"
        return await self._parse_message(channel, msg_id, url)

    # ---------- 核心解析流程 ----------
    async def _parse_message(self, entity, msg_id: int, url: str) -> ParseResult:
        client = await self._ensure_logged_in()

        # 取目标消息
        try:
            from telethon.errors import ChannelPrivateError

            try:
                target = await client.get_messages(entity, ids=msg_id)
            except ChannelPrivateError:
                raise ParseException("无法访问该频道/群组,请确认账号已加入")
        except ParseException:
            raise
        except Exception as e:
            raise ParseException(f"获取消息失败: {e}")

        if not target:
            raise ParseException("未找到该消息")

        # 回复引用
        reply_quote = await self._build_reply_quote(client, entity, target)

        # 先卡片预览,再发原始媒体
        card_contents: list[MediaContent] = []
        send_contents: list[MediaContent] = []
        info_parts: list[str] = []
        captions: list[str] = []

        if target.grouped_id is not None:  # type: ignore
            # 相册聚合
            messages = await self._fetch_album(client, entity, target)
            for m in messages:
                await self._process_message_media(
                    m, card_contents, send_contents, info_parts
                )
                if m.message:  # type: ignore
                    captions.append(m.message)  # type: ignore
        else:
            # 单条消息
            target_msg = target.message  # type: ignore
            await self._process_message_media(
                target, card_contents, send_contents, info_parts
            )
            if target_msg:
                captions.append(target_msg)

        # 合并文字(caption)
        combined = "\n".join(captions) if captions else ""
        full_text = combined or None

        # 构建发送分组
        # 两个 group: Group1 发卡片(force_merge=False), Group2 发原始媒体(走默认阈值)
        _heavy_types = (VideoContent, AudioContent, FileContent, DynamicContent)
        is_single_heavy = (
            len(send_contents) == 1
            and isinstance(send_contents[0], _heavy_types)
        )

        if send_contents:
            if is_single_heavy and not self.cfg.single_heavy_render_card:
                # 开关 OFF: 不渲染卡片, 直接发媒体+文字
                group_contents = list(send_contents)
                if full_text:
                    group_contents.append(TextContent(full_text))
                send_groups: list[SendGroup] = [
                    SendGroup(contents=group_contents)
                ]
            else:
                # 先独立发卡片, 再发原始媒体(走默认阈值决定是否合并转发)
                send_groups = [
                    SendGroup(contents=[], render_card=True, force_merge=False),
                    SendGroup(contents=send_contents, render_card=False),
                ]
        else:
            # 纯文字: 直接发文字, 不渲染卡片不合并转发
            text_contents: list[MediaContent] = (
                [TextContent(full_text)] if full_text else []
            )
            send_groups = [
                SendGroup(contents=text_contents)
            ]

        # 额外信息
        extra: dict[str, object] = {}
        if info_parts:
            extra["info"] = "\n".join(info_parts)

        # 转发
        repost = self._build_repost(target)

        # 转发消息: 媒体和文字属于原帖, 放入 repost 嵌套卡片
        if repost:
            if card_contents or full_text:
                repost.contents = card_contents  # type: ignore[method-assign]
                repost.text = full_text  # type: ignore[method-assign]
                card_contents = []
                full_text = None

        # 嵌套卡片渲染逻辑: render.py 仅渲染 repost 为嵌套卡片
        # 当有回复但无转发时,reply_quote 充当 repost
        # 当同时有回复和转发时,转发优先放 repost,回复引用退回文字拼接
        if reply_quote:
            if not repost:
                repost = reply_quote
                reply_quote = None
            else:
                reply_author = reply_quote.author.name if reply_quote.author else "引用"
                reply_text = reply_quote.text or ""
                if reply_text:
                    if full_text:
                        full_text += f"\n回复 {reply_author}: {reply_text}"
                    else:
                        full_text = f"回复 {reply_author}: {reply_text}"
                reply_quote = None

        # 作者/时间
        author = await self._build_author(target)
        target_date = target.date  # type: ignore[union-attr]
        timestamp = int(target_date.timestamp()) if target_date else None

        return self.result(
            title=None,
            text=full_text or None,
            author=author,
            contents=card_contents,
            send_groups=send_groups,
            timestamp=timestamp,
            url=url,
            repost=repost,
            extra=extra,
        )

    async def _process_message_media(
        self,
        msg,
        card_contents: list[MediaContent],
        send_contents: list[MediaContent],
        info_parts: list[str],
    ) -> None:
        """将媒体分流到卡片预览、原始发送、信息文本三处"""
        for item in await self._process_media(msg):
            if isinstance(item, TextContent):
                info_parts.append(item.text)
                continue
            send_contents.append(item)
            if isinstance(item, (ImageContent, VideoContent)):
                card_contents.append(item)
            elif isinstance(item, DynamicContent):
                # GIF 缩略图用于卡片
                thumb = await self._download_thumb(msg)
                if thumb:
                    card_contents.append(ImageContent(thumb))
            elif isinstance(item, FileContent):
                name = item.name or "未知文件"
                info_parts.append(f"文件: {name}")
            elif isinstance(item, AudioContent):
                if item.duration > 0:
                    minutes = int(item.duration) // 60
                    seconds = int(item.duration) % 60
                    info_parts.append(f"音频: {minutes}:{seconds:02d}")
                else:
                    info_parts.append("音频")

    async def _fetch_album(self, client, entity, target) -> list:
        """以 target 为中心取窗口拉取相册消息"""
        candidate_ids = list(
            range(target.id - self.ALBUM_WINDOW, target.id + self.ALBUM_WINDOW + 1)
        )
        fetched = await client.get_messages(entity, ids=candidate_ids)
        album: dict[int, object] = {}
        for m in fetched or []:
            if m and m.grouped_id == target.grouped_id:
                album[m.id] = m
        if not album:
            return [target]
        return [album[k] for k in sorted(album)]

    # ---------- 媒体分发与下载 ----------
    async def _process_media(self, msg) -> list[MediaContent]:
        """返回该消息的媒体内容列表,可能为空"""
        f = msg.file
        if f and f.size and f.size > self.cfg.max_size:
            if self.cfg.show_download_fail_tip:
                return [TextContent("此项媒体超过大小限制")]
            return []
        handler = self._get_media_handler(msg)
        if handler is None:
            return []
        content = await handler(msg)
        return [content] if content else []

    def _get_media_handler(self, msg):
        """顺序敏感:贴纸/GIF/圆视频是 document 子类,需先判断"""
        if msg.sticker:
            return self._handle_sticker
        if msg.gif:
            return self._handle_gif
        if msg.video_note:
            return self._handle_video_note
        if msg.photo:
            return self._handle_photo
        if msg.video:
            return self._handle_video
        if msg.voice:
            return self._handle_voice
        if msg.audio:
            return self._handle_audio
        if msg.document:
            return self._handle_file
        return None

    async def _download_media(self, msg) -> Path:
        """下载媒体到 cache_dir,返回 Path"""
        peer_key = (
            getattr(msg.peer_id, "channel_id", None) or msg.chat_id or 0
        )
        base = self.cfg.cache_dir / f"tg_{peer_key}_{msg.id}"
        path = await self.tgclient.download_media(msg, file=str(base))
        if not path:
            raise ParseException("媒体下载失败")
        return Path(str(path))  # type: ignore

    async def _download_thumb(self, msg) -> Path | None:
        """下载媒体缩略图(用于视频/GIF卡片封面),失败返回 None"""
        try:
            peer_key = (
                getattr(msg.peer_id, "channel_id", None) or msg.chat_id or 0
            )
            base = self.cfg.cache_dir / f"tg_{peer_key}_{msg.id}_thumb"
            path = await self.tgclient.download_media(
                msg, file=str(base), thumb=0
            )
            if path:
                return Path(str(path))  # type: ignore
        except Exception:
            pass
        return None

    # ---- 媒体类型处理 ----
    async def _handle_photo(self, msg) -> ImageContent:
        return ImageContent(await self._download_media(msg))

    async def _handle_video(self, msg) -> VideoContent:
        duration = float(msg.file.duration or 0.0) if msg.file else 0.0
        video_path = await self._download_media(msg)
        cover_path = await self._download_thumb(msg)
        return VideoContent(video_path, cover_path, duration)

    async def _handle_video_note(self, msg) -> VideoContent:
        """圆视频"""
        duration = float(msg.file.duration or 0.0) if msg.file else 0.0
        video_path = await self._download_media(msg)
        cover_path = await self._download_thumb(msg)
        return VideoContent(video_path, cover_path, duration)

    async def _handle_voice(self, msg) -> AudioContent:
        duration = float(msg.file.duration or 0.0) if msg.file else 0.0
        return AudioContent(await self._download_media(msg), duration)

    async def _handle_audio(self, msg) -> AudioContent:
        duration = float(msg.file.duration or 0.0) if msg.file else 0.0
        return AudioContent(await self._download_media(msg), duration)

    async def _handle_file(self, msg) -> FileContent:
        name = msg.file.name if msg.file else None
        return FileContent(await self._download_media(msg), name)

    async def _handle_sticker(self, msg) -> FileContent:
        """所有贴纸格式 → FileContent"""
        name = msg.file.name if msg.file else None
        return FileContent(await self._download_media(msg), name)

    async def _handle_gif(self, msg) -> DynamicContent:
        """GIF → DynamicContent"""
        return DynamicContent(await self._download_media(msg))

    # ---------- 作者与转发 ----------
    async def _build_author(self, msg) -> Author:
        sender = msg.sender
        name = "未知"
        avatar: Path | None = None
        if sender:
            name = self._extract_entity_name(sender)
            try:
                if getattr(sender, "photo", None):
                    p = await self.tgclient.download_profile_photo(
                        sender, file=str(self.cfg.cache_dir)
                    )
                    if p:
                        avatar = Path(p)
            except Exception:
                avatar = None
        return Author(name=name, avatar=avatar)

    def _build_repost(self, msg):
        """转发消息 → 嵌套 ParseResult"""
        fwd = msg.fwd_from
        if not fwd:
            return None
        sender_name: str | None = None
        try:
            if fwd.sender:
                sender_name = self._extract_entity_name(fwd.sender)
        except Exception:
            pass
        name = (
            sender_name
            or getattr(fwd, "post_author", None)
            or getattr(fwd, "from_name", None)
            or getattr(fwd, "sender_name", None)
            or "未知"
        )
        timestamp = int(fwd.date.timestamp()) if fwd.date else None
        orig_url = self._build_repost_url(fwd)
        author = Author(name=name)
        return self.result(author=author, timestamp=timestamp, url=orig_url, contents=[])

    # ---------- 回复引用 ----------
    async def _build_reply_quote(self, client, entity, target) -> "ParseResult | None":
        """获取被回复消息,构建嵌套 ParseResult。"""
        reply_to = getattr(target, "reply_to", None)
        if not reply_to:
            return None
        reply_msg_id = getattr(reply_to, "reply_to_msg_id", None)
        if not reply_msg_id:
            return None

        reply_msg = None
        try:
            reply_msg = await client.get_messages(entity, ids=reply_msg_id)
        except Exception:
            pass

        # 优先用完整消息文本,其次回退到 quote_text
        reply_text: str | None = None
        if reply_msg:
            reply_text = reply_msg.message or ""
            if not reply_text and getattr(reply_msg, "media", None):
                reply_text = "[媒体内容]"

        if not reply_text:
            quote_text = getattr(reply_to, "quote_text", None)
            if quote_text:
                reply_text = quote_text

        if not reply_text:
            return None

        # 作者/时间
        author: Author | None = None
        if reply_msg:
            author = await self._build_author(reply_msg)
        if author is None:
            author = Author(name="引用消息")

        timestamp = None
        if reply_msg and reply_msg.date:
            timestamp = int(reply_msg.date.timestamp())

        return self.result(
            author=author,
            text=reply_text,
            timestamp=timestamp,
            contents=[],
        )

    @staticmethod
    def _build_repost_url(fwd) -> "str | None":
        """根据 fwd.from_id 构建原帖链接,仅 PeerChannel 可用"""
        from_id = getattr(fwd, "from_id", None)
        channel_post = getattr(fwd, "channel_post", None)
        if not from_id or not channel_post:
            return None
        # PeerChannel 有 channel_id
        channel_id = getattr(from_id, "channel_id", None)
        if channel_id:
            return f"https://t.me/c/{channel_id}/{channel_post}"
        return None

    @staticmethod
    def _extract_entity_name(entity) -> str:
        """从用户/频道/群组提取显示名称"""
        name = getattr(entity, "title", None)
        if name:
            return name
        name = getattr(entity, "username", None)
        if name:
            return name
        first = getattr(entity, "first_name", None) or ""
        last = getattr(entity, "last_name", None) or ""
        full = f"{first} {last}".strip()
        if full:
            return full
        return str(getattr(entity, "id", "未知"))


    # ---------- Telegraph  ----------
    _TELEGRAPH_BLOCK_TAGS: ClassVar[set[str]] = {
        "p", "h3", "h4", "blockquote", "ul", "ol", "li",
        "figure", "figcaption", "aside", "pre",
    }

    @handle("telegra.ph", r"telegra\.ph/(?P<path>[^/\s?#]+)")
    async def _parse_telegraph(self, searched: Match[str]) -> ParseResult:
        """解析 telegra.ph 文章"""
        path = searched.group("path")
        url = f"https://telegra.ph/{path}"
        return await self._parse_telegraph_page(path, url)

    async def _parse_telegraph_page(self, path: str, url: str) -> ParseResult:
        """调用 Telegraph API 解析文章内容"""
        api_url = f"https://api.telegra.ph/getPage/{path}?return_content=true"
        try:
            async with self.session.get(
                api_url, headers=self.headers, proxy=self.proxy
            ) as resp:
                if resp.status >= 400:
                    raise ParseException(
                        f"Telegraph API 请求失败: HTTP {resp.status}"
                    )
                data = await resp.json()
        except ClientError as e:
            raise ParseException(f"Telegraph API 请求失败: {e}")

        if not data.get("ok"):
            raise ParseException(
                f"Telegraph API 返回错误: {data.get('error', '未知错误')}"
            )

        page = data["result"]
        title = page.get("title")
        author_name = page.get("author_name") or "Anonymous"
        content = page.get("content") or []

        # 遍历节点树,文字累积,遇到图片/视频切分
        contents: list[MediaContent] = []
        text_buf: list[str] = []
        self._walk_telegraph_nodes(content, contents, text_buf)
        self._flush_telegraph_text(contents, text_buf)

        author = Author(name=author_name)

        return self.result(
            title=title,
            text=None,
            author=author,
            contents=contents,
            url=url,
        )

    def _walk_telegraph_nodes(
        self,
        nodes: list,
        contents: list[MediaContent],
        text_buf: list[str],
    ) -> None:
        """深度优先遍历节点树,文字累积,媒体切分"""
        for node in nodes:
            if isinstance(node, str):
                text_buf.append(node)
                continue
            if not isinstance(node, dict):
                continue
            tag = node.get("tag", "")
            attrs = node.get("attrs") or {}

            if tag == "img":
                self._flush_telegraph_text(contents, text_buf)
                src = attrs.get("src", "")
                if src:
                    img_url = self._normalize_telegraph_url(src)
                    task = self.downloader.download_img(
                        img_url, headers=self.headers, proxy=self.proxy
                    )
                    contents.append(ImageContent(task))
            elif tag == "video":
                self._flush_telegraph_text(contents, text_buf)
                src = attrs.get("src", "")
                if src:
                    vid_url = self._normalize_telegraph_url(src)
                    task = self.downloader.download_video(
                        vid_url, headers=self.headers, proxy=self.proxy
                    )
                    contents.append(VideoContent(task))
            elif tag == "br":
                text_buf.append("\n")
            elif tag == "hr":
                text_buf.append("\n———\n")
            elif tag == "iframe":
                src = attrs.get("src", "")
                if src:
                    if text_buf and not text_buf[-1].endswith("\n"):
                        text_buf.append("\n")
                    text_buf.append(f"[嵌入内容] {src}")
            else:
                # 块级元素前补换行
                if (
                    tag in self._TELEGRAPH_BLOCK_TAGS
                    and text_buf
                    and not text_buf[-1].endswith("\n")
                ):
                    text_buf.append("\n")
                children = node.get("children") or []
                self._walk_telegraph_nodes(children, contents, text_buf)
                if tag in self._TELEGRAPH_BLOCK_TAGS:
                    text_buf.append("\n")

    @staticmethod
    def _flush_telegraph_text(
        contents: list[MediaContent], text_buf: list[str]
    ) -> None:
        """将缓冲文字合并为 TextContent 追加到 contents,并清空缓冲"""
        text = "".join(text_buf).strip()
        if text:
            contents.append(TextContent(text))
        text_buf.clear()

    @staticmethod
    def _normalize_telegraph_url(src: str) -> str:
        """将 Telegraph 相对 URL 补全为绝对 URL"""
        if src.startswith("http://") or src.startswith("https://"):
            return src
        if src.startswith("/"):
            return f"https://telegra.ph{src}"
        return f"https://telegra.ph/{src}"
