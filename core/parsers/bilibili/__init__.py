from __future__ import annotations

import asyncio
from re import Match
from typing import Any, ClassVar

from aiohttp import ClientError
from astrbot.api import logger

from data.plugins.astrbot_plugin_parser.core.data import (
    ImageContent,
    MediaContent,
    Platform,
)
from data.plugins.astrbot_plugin_parser.core.download import Downloader
from data.plugins.astrbot_plugin_parser.core.exception import (
    DownloadException,
    DurationLimitException,
    ParseException,
)
from data.plugins.astrbot_plugin_parser.core.parsers.base import BaseParser, handle
from data.plugins.astrbot_plugin_parser.core.parsers.bilibili import (
    BilibiliParser as _BaseBilibiliParser,
)

from ...config import PluginConfig
from . import endpoints
from .credential import BiliCredential
from .models import (
    PageInfo,
    parse_article,
    parse_bangumi,
    parse_favlist,
    parse_live,
    parse_opus_data,
    parse_video_data,
)
from .stream import calculate_fnval, pick_durl, select_streams

# 画质别名 -> qn (兼容原插件 _720P 写法 与 rconsole qn 数字写法)
_QUALITY_ALIAS: dict[str, int] = {
    "_360P": 16,
    "_480P": 32,
    "_720P": 64,
    "_720P60": 74,
    "_1080P": 80,
    "_1080P_PLUS": 112,
    "_1080P_60": 116,
    "_4K": 120,
    "HDR": 125,
    "DOLBY": 126,
    "_8K": 127,
    "AI_REPAIR": 80,
}


def _resolve_qn(raw: Any, default: int = 64) -> int:
    if raw is None:
        return default
    s = str(raw).strip()
    if s.isdigit():
        return int(s)
    return _QUALITY_ALIAS.get(s.upper(), default)


class BilibiliParser(_BaseBilibiliParser):
    """哔哩哔哩解析器 - 覆盖原插件解析器

    继承原 BilibiliParser 只为通过原插件 `登录B站` 指令的 isinstance 检查
    """

    platform: ClassVar[Platform] = Platform(name="bilibili", display_name="B站")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        # 跳过原 BilibiliParser.__init__ (bilibili_api 初始化), 直接走 BaseParser
        BaseParser.__init__(self, config, downloader)
        self.mycfg = getattr(config.parser, "bilibili", None)
        self.headers.update(
            {
                "Referer": "https://www.bilibili.com/",
                "Origin": "https://www.bilibili.com",
            }
        )
        self.qn = _resolve_qn(getattr(self.mycfg, "video_quality", None))
        self.preferred_codec = (
            getattr(self.mycfg, "video_codec", None) or "auto"
        )
        self.credential = BiliCredential(self)
        # 兼容原插件 `登录B站` 指令: 它调用 parser.login.login_with_qrcode/check_qr_state
        self.login = self.credential
        # 已有 SESSDATA 时同步到请求头
        if self.credential.cookie_header:
            self.headers["cookie"] = self.credential.cookie_header

    # ==================== 工具 ====================

    def bili_headers(self) -> dict[str, str]:
        """B 站接口请求头 (含 Cookie)"""
        headers = dict(self.headers)
        cookie = self.credential.cookie_header
        if cookie:
            headers["cookie"] = cookie
        return headers

    @property
    def max_duration(self) -> float:
        base = getattr(self.cfg, "base_config", None)
        val = getattr(self.cfg, "max_duration", None) or getattr(base, "max_duration", None)
        return float(val) if val else 600.0

    async def _api_get(self, url: str, *, signed: bool = False) -> dict[str, Any]:
        """GET JSON 接口, code != 0 时抛异常"""
        if signed:
            # WBI 签名: 把 query 参数并入签名
            from urllib.parse import parse_qsl, urlparse

            parsed = urlparse(url)
            params = dict(parse_qsl(parsed.query, keep_blank_values=True))
            signed_params = await self.credential.sign_params(params)
            from urllib.parse import urlencode

            base = parsed._replace(query="").geturl()
            url = f"{base}?{urlencode(signed_params)}"

        try:
            async with self.session.get(url, headers=self.bili_headers()) as resp:
                if resp.status >= 400:
                    raise ParseException(f"B 站接口 HTTP {resp.status}")
                data = await resp.json(content_type=None)
        except (ClientError, asyncio.TimeoutError) as e:
            raise ParseException(f"请求 B 站接口失败: {e}") from e

        if not isinstance(data, dict):
            raise ParseException("B 站接口返回异常")
        code = data.get("code")
        if code not in (0, None):
            raise ParseException(f"B 站接口错误: {data.get('message') or code}")
        return data

    async def _get_video_info(self, *, bvid: str | None = None, avid: int | None = None):
        query = f"bvid={bvid}" if bvid else f"aid={avid}"
        data = await self._api_get(f"{endpoints.VIDEO_INFO}?{query}")
        payload = data.get("data")
        if not payload:
            raise ParseException("视频不存在或已失效")
        return parse_video_data(payload)

    async def _resolve_cid(self, video, page_index: int = 0) -> int:
        if page_index == 0 and video.cid:
            return video.cid
        if video.pages and page_index < len(video.pages):
            cid = int(video.pages[page_index].get("cid") or 0)
            if cid:
                return cid
        data = await self._api_get(endpoints.BVID_PAGELIST.format(bvid=video.bvid))
        pages = data.get("data") or []
        if not pages:
            raise ParseException("无法获取视频分P信息")
        idx = min(page_index, len(pages) - 1)
        return int(pages[idx].get("cid") or 0)

    async def _fetch_playurl(self, *, bvid: str, cid: int, qn: int | None = None) -> dict[str, Any]:
        qn = qn if qn is not None else self.qn
        fnval, fourk = calculate_fnval(qn)
        url = endpoints.PLAY_STREAM.format(
            cid=cid, bvid=bvid, qn=qn, fnval=fnval, fourk=fourk
        )
        data = await self._api_get(url, signed=True)
        payload = data.get("data") or {}
        if not payload:
            raise ParseException("获取播放流失败")
        return payload

    def _make_video_task(self, video, page_info, v_url: str, a_url: str | None):
        """构造下载任务 (与原 parser 相同的下载路径)"""

        async def download_video():
            output_path = self.cfg.cache_dir / f"{video.bvid}-{page_info.index + 1}.mp4"
            if output_path.exists():
                return output_path
            if page_info.duration > self.max_duration:
                raise DurationLimitException
            if a_url is not None:
                return await self.downloader.download_av_and_merge(
                    v_url,
                    a_url,
                    output_path=output_path,
                    headers=self.headers,
                    proxy=self.proxy,
                )
            return await self.downloader.streamd(
                v_url,
                file_name=output_path.name,
                headers=self.headers,
                proxy=self.proxy,
            )

        return asyncio.create_task(download_video())

    # ==================== 链接匹配 ====================

    @handle("b23.tv", r"b23\.tv/[A-Za-z\d\._?%&+\-=/#]+")
    @handle("bili2233", r"bili2233\.cn/[A-Za-z\d\._?%&+\-=/#]+")
    async def _parse_short_link(self, searched: Match[str]):
        """解析短链"""
        url = f"https://{searched.group(0)}"
        return await self.parse_with_redirect(url)

    @handle("BV", r"^(?P<bvid>BV[0-9a-zA-Z]{10})(?:\s)?(?P<page_num>\d{1,3})?$")
    @handle(
        "/BV",
        r"bilibili\.com(?:/video)?/(?P<bvid>BV[0-9a-zA-Z]{10})(?:\?p=(?P<page_num>\d{1,3}))?",
    )
    async def _parse_bv(self, searched: Match[str]):
        bvid = str(searched.group("bvid"))
        page_num = int(searched.group("page_num") or 1)
        return await self.parse_video(bvid=bvid, page_num=page_num)

    @handle("bm", r"^bm(?P<bvid>BV[0-9a-zA-Z]{10})(?:\s(?P<page_num>\d{1,3}))?$")
    async def _parse_bv_bm(self, searched: Match[str]):
        """只取音频流"""
        bvid = searched.group("bvid")
        page = int(searched.group("page_num") or 1)
        video = await self._get_video_info(bvid=bvid)
        page_info = video.page_info(page)
        cid = await self._resolve_cid(video, page_info.index)
        payload = await self._fetch_playurl(bvid=bvid, cid=cid)

        a_url = self._extract_audio_url(payload)
        if not a_url:
            raise ParseException("未找到音频链接")
        audio = self.create_audio_content(a_url)
        return self.result(
            title=f"BiliBili_audio_{bvid}",
            contents=[audio],
            url=a_url,
        )

    @handle("av", r"^av(?P<avid>\d{6,})(?:\s)?(?P<page_num>\d{1,3})?$")
    @handle(
        "/av",
        r"bilibili\.com(?:/video)?/av(?P<avid>\d{6,})(?:\?p=(?P<page_num>\d{1,3}))?",
    )
    async def _parse_av(self, searched: Match[str]):
        avid = int(searched.group("avid"))
        page_num = int(searched.group("page_num") or 1)
        return await self.parse_video(avid=avid, page_num=page_num)

    @handle("/dynamic/", r"bilibili\.com/dynamic/(?P<dynamic_id>\d+)")
    @handle("t.bili", r"t\.bilibili\.com/(?P<dynamic_id>\d+)")
    async def _parse_dynamic(self, searched: Match[str]):
        dynamic_id = int(searched.group("dynamic_id"))
        return await self.parse_dynamic(dynamic_id)

    @handle("live.bili", r"live\.bilibili\.com/(?P<room_id>\d+)")
    async def _parse_live(self, searched: Match[str]):
        room_id = int(searched.group("room_id"))
        return await self.parse_live(room_id)

    @handle("/favlist", r"favlist\?fid=(?P<fav_id>\d+)")
    async def _parse_favlist(self, searched: Match[str]):
        fav_id = int(searched.group("fav_id"))
        return await self.parse_favlist(fav_id)

    @handle("/read/", r"bilibili\.com/read/cv(?P<read_id>\d+)")
    async def _parse_read(self, searched: Match[str]):
        read_id = int(searched.group("read_id"))
        return await self.parse_article(read_id)

    @handle("/opus/", r"bilibili\.com/opus/(?P<opus_id>\d+)")
    async def _parse_opus(self, searched: Match[str]):
        opus_id = int(searched.group("opus_id"))
        return await self.parse_dynamic(opus_id)

    @handle("bangumi/play/ep", r"bilibili\.com/bangumi/play/ep(?P<ep_id>\d+)")
    async def _parse_bangumi_ep(self, searched: Match[str]):
        ep_id = int(searched.group("ep_id"))
        return await self.parse_bangumi_ep(ep_id)

    @handle("bangumi/play/ss", r"bilibili\.com/bangumi/play/ss(?P<ss_id>\d+)")
    async def _parse_bangumi_ss(self, searched: Match[str]):
        ss_id = int(searched.group("ss_id"))
        return await self.parse_bangumi_ss(ss_id)

    # ==================== 解析实现 ====================

    async def parse_video(
        self,
        *,
        bvid: str | None = None,
        avid: int | None = None,
        page_num: int = 1,
    ):
        """视频解析 -> 原 parser 卡片/视频返回格式"""
        video = await self._get_video_info(bvid=bvid, avid=avid)
        page_info = video.page_info(page_num)
        text = f"简介: {video.desc}" if video.desc else None
        author = self.create_author(video.owner_name, video.owner_face)

        url = f"https://www.bilibili.com/video/{video.bvid}"
        if page_info.index > 0:
            url += f"?p={page_info.index + 1}"

        # AI 总结 (有登录态才尝试)
        ai_summary = ""
        if self.credential.sessdata:
            try:
                params = await self.credential.sign_params(
                    {
                        "bvid": video.bvid,
                        "cid": page_info.cid or await self._resolve_cid(video, page_info.index),
                        "up_mid": video.owner_mid,
                    }
                )
                from urllib.parse import urlencode

                data = await self._api_get(
                    f"{endpoints.AI_CONCLUSION}?{urlencode(params)}"
                )
                model_result = (data.get("data") or {}).get("model_result") or {}
                summary = model_result.get("summary")
                ai_summary = (
                    f"AI总结: {summary}" if summary else "该视频暂不支持AI总结"
                )
            except Exception:
                ai_summary = "哔哩哔哩 cookie 未配置或失效, 无法使用 AI 总结"

        cid = page_info.cid or await self._resolve_cid(video, page_info.index)
        payload = await self._fetch_playurl(bvid=video.bvid, cid=cid)
        v_url, a_url = self._select_from_payload(payload)

        video_task = self._make_video_task(video, page_info, v_url, a_url)
        video_content = self.create_video_content_by_task(
            video_task,
            page_info.cover,
            page_info.duration,
        )

        return self.result(
            url=url,
            title=page_info.title,
            timestamp=page_info.timestamp,
            text=text,
            author=author,
            contents=[video_content],
            extra={"info": ai_summary},
        )

    async def parse_dynamic(self, dynamic_id: int):
        """动态/图文解析 (opus detail 接口)"""
        data = await self._api_get(endpoints.OPUS_DETAIL.format(opus_id=dynamic_id))
        item = ((data.get("data") or {}).get("item")) or {}
        if not item:
            raise ParseException("获取动态信息失败")

        opus = parse_opus_data(item)
        author = self.create_author(opus.author_name, opus.author_face)

        contents: list[MediaContent] = []
        current_text = ""
        for para in opus.paragraphs:
            if para.kind == "image" and para.url:
                contents.append(
                    self.create_graphics_content(para.url, current_text.strip() or None)
                )
                current_text = ""
            elif para.kind == "divider":
                current_text += "\n---\n"
            elif para.text:
                current_text += ("\n" if current_text else "") + para.text

        return self.result(
            title=opus.title or None,
            text=current_text.strip() or None,
            timestamp=opus.timestamp or None,
            author=author,
            contents=contents,
        )

    async def parse_live(self, room_id: int):
        """直播解析"""
        data = await self._api_get(endpoints.LIVE_ROOM_FULL.format(room_id=room_id))
        info = data.get("data") or {}
        if not info:
            raise ParseException("获取直播间信息失败")
        live = parse_live(info, room_id)

        contents: list[MediaContent] = []
        for img_url in (live.cover, live.keyframe):
            if img_url:
                contents.append(
                    ImageContent(
                        self.downloader.download_img(
                            img_url, headers=self.headers, proxy=self.proxy
                        )
                    )
                )

        author = self.create_author(live.uname, live.face)
        detail_lines = []
        if live.description:
            detail_lines.append(f"简述: {live.description}")
        if live.tags:
            detail_lines.append(f"标签: {live.tags}")
        if live.parent_area_name or live.area_name:
            detail_lines.append(
                f"分区: {live.parent_area_name}-{live.area_name}".strip("-")
            )
        if live.live_time:
            detail_lines.append(f"直播时间: {live.live_time}")

        url = (
            "https://www.bilibili.com/blackboard/live/live-activity-player.html"
            f"?enterTheRoom=0&cid={room_id}"
        )
        return self.result(
            url=url,
            title=f"直播 - {live.title}",
            text="\n".join(detail_lines) or None,
            contents=contents,
            author=author,
        )

    async def parse_favlist(self, fav_id: int):
        """收藏夹解析"""
        data = await self._api_get(endpoints.FAV_CONTENT.format(fid=fav_id))
        payload = data.get("data") or {}
        if payload.get("medias") is None:
            raise ParseException("收藏夹内容为空, 或被风控")
        fav = parse_favlist(payload)

        return self.result(
            title=f"收藏夹 - {fav.title}",
            timestamp=fav.ctime or None,
            author=self.create_author(fav.upper_name, fav.upper_face),
            contents=[
                self.create_graphics_content(m.cover, m.desc) for m in fav.medias
            ],
        )

    async def parse_article(self, cvid: int):
        """专栏解析"""
        info_resp = await self._api_get(endpoints.ARTICLE_INFO.format(cvid=cvid))
        viewinfo = info_resp.get("data") or {}
        if not viewinfo:
            raise ParseException("获取专栏信息失败")

        view: dict[str, Any] | None = None
        try:
            view_resp = await self._api_get(endpoints.ARTICLE_VIEW.format(cvid=cvid))
            view = view_resp.get("data") or {}
        except ParseException:
            logger.warning(f"[BiliExt] 获取专栏正文失败, 只返回标题与图片 cv{cvid}")

        article = parse_article(viewinfo, view, cvid)
        author = self.create_author(article.author_name, article.author_face)

        contents: list[MediaContent] = []
        current_text = ""
        # 正文文本整体作为 text, 图片按 GraphicsContent 附前文
        body = article.content_text or article.summary or ""
        for img_url in article.image_urls:
            if not current_text and body:
                current_text = body
                body = ""
            contents.append(
                self.create_graphics_content(img_url, current_text.strip() or None)
            )
            current_text = ""
        remaining = (current_text + "\n" + body).strip() if (current_text or body) else ""

        return self.result(
            title=article.title,
            text=remaining or None,
            timestamp=article.publish_time or None,
            author=author,
            contents=contents,
        )

    async def parse_bangumi_ep(self, ep_id: int):
        """番剧单集解析 (rconsole biliEpInfo 路线)"""
        data = await self._api_get(endpoints.EP_INFO.format(ep_id=ep_id))
        result = data.get("result") or {}
        if not result:
            raise ParseException("获取番剧信息失败")
        return await self._build_bangumi_result(parse_bangumi(result, ep_id))

    async def parse_bangumi_ss(self, ss_id: int):
        """番剧季度解析 -> 取第一集"""
        data = await self._api_get(endpoints.SS_INFO.format(ss_id=ss_id))
        result = data.get("result") or {}
        main_eps = (result.get("main_section") or {}).get("episodes") or []
        episodes = main_eps or result.get("episodes") or []
        if not episodes:
            raise ParseException("该番剧季度没有可解析的集数")
        first = episodes[0]
        ep_id = int(first.get("id") or first.get("ep_id") or 0)
        if not ep_id:
            raise ParseException("无法确定番剧集数")
        return await self.parse_bangumi_ep(ep_id)

    async def _build_bangumi_result(self, bangumi):
        """番剧结果 -> 原 parser 卡片格式; 时长合规时附带视频"""
        author = self.create_author(bangumi.title, bangumi.cover or None)
        contents: list[MediaContent] = []
        if bangumi.cover:
            contents.append(
                ImageContent(
                    self.downloader.download_img(
                        bangumi.cover, headers=self.headers, proxy=self.proxy
                    )
                )
            )

        stat = bangumi.stat or {}
        stat_bits = []
        for label, key in (
            ("播放", "views"), ("弹幕", "danmakus"), ("点赞", "likes"),
            ("追番", "favorites"), ("收藏", "favorite"),
        ):
            if stat.get(key) is not None:
                stat_bits.append(f"{label} {stat[key]}")

        lines = [f"类型: {bangumi.type_name}"]
        if bangumi.ep_title or bangumi.ep_long_title:
            lines.append(
                f"本集: {bangumi.ep_title} {bangumi.ep_long_title}".strip()
            )
        if bangumi.rating_score is not None:
            lines.append(
                f"评分: {bangumi.rating_score} / {bangumi.rating_count or '-'}"
            )
        if bangumi.update_info:
            lines.append(bangumi.update_info)
        if stat_bits:
            lines.append(" | ".join(stat_bits))

        url = f"https://www.bilibili.com/bangumi/play/ep{bangumi.ep_id}"
        title = f"{bangumi.title} - 第{bangumi.ep_title or bangumi.ep_id}话"
        duration = bangumi.duration_ms / 1000 if bangumi.duration_ms else 0.0

        # 超时长: 只展示信息
        if duration and duration > self.max_duration:
            lines.append(
                f"注意: 本集时长约 {int(duration // 60)} 分钟, 超过下载限制, 仅展示信息"
            )
            return self.result(
                url=url, title=title, text="\n".join(lines),
                author=author, contents=contents,
            )

        # 尝试下载本集 (pgc playurl, rconsole bangumi 路线)
        if bangumi.bvid and bangumi.cid:
            try:
                payload = await self._fetch_bangumi_playurl(bangumi.ep_id, bangumi.cid)
                v_url, a_url = self._select_from_payload(payload)
                page_info = PageInfo(
                    index=0, title=title, duration=int(duration),
                    timestamp=0, cover=bangumi.cover or None, cid=bangumi.cid,
                )
                fake_video = type("V", (), {"bvid": bangumi.bvid})()
                video_task = self._make_video_task(fake_video, page_info, v_url, a_url)
                contents.append(
                    self.create_video_content_by_task(
                        video_task, bangumi.cover or None, duration
                    )
                )
            except Exception as e:
                logger.warning(f"[BiliExt] 番剧下载失败, 只展示信息: {e}")

        return self.result(
            url=url, title=title, text="\n".join(lines),
            author=author, contents=contents,
        )

    async def _fetch_bangumi_playurl(self, ep_id: int, cid: int) -> dict[str, Any]:
        qn = self.qn
        fnval, fourk = calculate_fnval(qn)
        url = endpoints.BANGUMI_STREAM.format(
            ep_id=ep_id, cid=cid, qn=qn, fnval=fnval, fourk=fourk
        )
        data = await self._api_get(url)
        result = data.get("result") or data.get("data") or {}
        if not result:
            raise ParseException("获取番剧播放流失败")
        return result

    # ==================== 播放流提取 ====================

    def _select_from_payload(self, payload: dict[str, Any]) -> tuple[str, str | None]:
        """从 playurl 返回中选择 (视频URL, 音频URL|None); 兼容 dash / durl"""
        dash = payload.get("dash")
        if dash and dash.get("video"):
            try:
                return select_streams(
                    dash, qn=self.qn, preferred_codec=self.preferred_codec
                )
            except ValueError as e:
                raise DownloadException(str(e)) from e
        durl = payload.get("durl")
        if durl:
            # 试看/完整单文件流
            try:
                return pick_durl(durl), None
            except ValueError as e:
                raise DownloadException(str(e)) from e
        raise DownloadException("未找到可下载的视频流（可能需要大会员或编码不匹配）")

    def _extract_audio_url(self, payload: dict[str, Any]) -> str | None:
        dash = payload.get("dash") or {}
        audios = dash.get("audio") or []
        if not audios:
            return None
        best = max(audios, key=lambda a: a.get("bandwidth") or 0)
        return best.get("baseUrl") or best.get("base_url")
