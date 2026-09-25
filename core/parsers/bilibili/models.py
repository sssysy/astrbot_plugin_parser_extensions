import html as html_mod
import re
from dataclasses import dataclass, field
from typing import Any


def _decode_html(text: str) -> str:
    """解码 HTML 实体 (参照 rconsole decodeHtmlEntities)"""
    if not text:
        return ""
    return html_mod.unescape(text)


# ==================== 视频 ====================


@dataclass
class PageInfo:
    index: int
    title: str
    duration: int
    timestamp: int
    cover: str | None = None
    cid: int = 0


@dataclass
class VideoData:
    bvid: str
    aid: int
    title: str
    desc: str
    duration: int
    pic: str
    pubdate: int
    owner_name: str
    owner_face: str
    owner_mid: int
    cid: int
    pages: list[dict[str, Any]] = field(default_factory=list)
    stat: dict[str, Any] = field(default_factory=dict)

    def page_info(self, page_num: int = 1) -> PageInfo:
        """取指定分P信息; 多P时标题拼接分P名"""
        title = self.title
        duration = self.duration
        cover = self.pic
        timestamp = self.pubdate
        cid = self.cid
        idx = 0

        if len(self.pages) > 1:
            idx = (page_num - 1) % len(self.pages)
            page = self.pages[idx] or {}
            title += f" | 分集 - {page.get('part') or ''}"
            duration = int(page.get("duration") or duration)
            cover = page.get("first_frame") or self.pic
            timestamp = int(page.get("ctime") or timestamp)
            cid = int(page.get("cid") or cid)

        return PageInfo(
            index=idx,
            title=title.strip(),
            duration=duration,
            timestamp=timestamp,
            cover=cover,
            cid=cid,
        )


def parse_video_data(data: dict[str, Any]) -> VideoData:
    owner = data.get("owner") or {}
    pages = data.get("pages") or []
    first_cid = 0
    if pages:
        first_cid = int(pages[0].get("cid") or 0)
    return VideoData(
        bvid=str(data.get("bvid") or ""),
        aid=int(data.get("aid") or 0),
        title=str(data.get("title") or ""),
        desc=str(data.get("desc") or ""),
        duration=int(data.get("duration") or 0),
        pic=str(data.get("pic") or ""),
        pubdate=int(data.get("pubdate") or 0),
        owner_name=str(owner.get("name") or ""),
        owner_face=str(owner.get("face") or ""),
        owner_mid=int(owner.get("mid") or 0),
        cid=int(data.get("cid") or first_cid),
        pages=pages,
        stat=data.get("stat") or {},
    )


# ==================== 动态 / 图文 (opus detail) ====================


def _text_from_nodes(nodes: list[dict[str, Any]]) -> str:
    """从富文本节点提取纯文本 (参照 rconsole extractTextFromNodes)"""
    text = ""
    for node in nodes or []:
        ntype = node.get("type")
        if ntype == "TEXT_NODE_TYPE_WORD" and node.get("word"):
            text += (node["word"].get("words") or "")
        elif ntype == "TEXT_NODE_TYPE_RICH" and node.get("rich"):
            rich = node["rich"]
            if rich.get("type") == "RICH_TEXT_NODE_TYPE_WEB":
                link_text = rich.get("text") or "网页链接"
                jump = rich.get("jump_url") or ""
                text += f"🔗 {link_text}({jump})" if jump else link_text
            else:
                text += rich.get("text") or rich.get("orig_text") or ""
        elif ntype == "TEXT_NODE_TYPE_FORMULA" and node.get("formula"):
            text += node["formula"].get("latex_content") or ""
    return _decode_html(text)


@dataclass
class ParaNode:
    """段落节点: kind = text | image | divider | quote | list | link | code"""
    kind: str
    text: str = ""
    url: str = ""


@dataclass
class OpusData:
    title: str = ""
    author_name: str = ""
    author_face: str = ""
    timestamp: int = 0
    paragraphs: list[ParaNode] = field(default_factory=list)

    @property
    def image_urls(self) -> list[str]:
        return [p.url for p in self.paragraphs if p.kind == "image" and p.url]

    @property
    def text(self) -> str:
        parts = [p.text for p in self.paragraphs if p.kind in ("text", "quote", "list", "link", "code") and p.text]
        return "\n".join(parts).strip()


def parse_opus_data(item: dict[str, Any]) -> OpusData:
    """解析 opus/detail 返回的 item (rconsole getDynamic 的段落化版本)"""
    result = OpusData()
    paragraphs: list[ParaNode] = []

    for module in item.get("modules") or []:
        mtype = module.get("module_type")

        if mtype == "MODULE_TYPE_AUTHOR" and module.get("module_author"):
            author = module["module_author"]
            result.author_name = str(author.get("name") or "")
            result.author_face = str(author.get("face") or "")
            try:
                result.timestamp = int(float(author.get("pub_ts") or 0))
            except (TypeError, ValueError):
                result.timestamp = 0

        if mtype == "MODULE_TYPE_TITLE" and module.get("module_title"):
            result.title = _decode_html(module["module_title"].get("text") or "")

        if mtype == "MODULE_TYPE_CONTENT" and module.get("module_content"):
            for para in module["module_content"].get("paragraphs") or []:
                ptype = para.get("para_type")
                # 1 文本 / 4 引用
                if ptype in (1, 4) and para.get("text"):
                    content = _text_from_nodes(para["text"].get("nodes") or [])
                    content = content.strip()
                    if content:
                        paragraphs.append(
                            ParaNode(kind="quote" if ptype == 4 else "text",
                                     text=f"「{content}」" if ptype == 4 else content)
                        )
                # 2 图片
                elif ptype == 2 and para.get("pic"):
                    for pic in para["pic"].get("pics") or []:
                        url = pic.get("url")
                        if url:
                            paragraphs.append(ParaNode(kind="image", url=url))
                # 3 分割线
                elif ptype == 3:
                    paragraphs.append(ParaNode(kind="divider", text="---"))
                # 5 列表
                elif ptype == 5 and para.get("list"):
                    for entry in para["list"].get("items") or []:
                        line = _text_from_nodes(entry.get("nodes") or []).strip()
                        if line:
                            paragraphs.append(ParaNode(kind="list", text=f"• {line}"))
                # 6 链接卡片
                elif ptype == 6 and para.get("link_card"):
                    card = para["link_card"].get("card") or {}
                    card_text = ""
                    card_url = ""
                    ugc = card.get("ugc") or {}
                    common = card.get("common") or {}
                    vote = card.get("vote") or {}
                    if card.get("type") == "LINK_CARD_TYPE_UGC":
                        card_text = ugc.get("title") or "视频链接"
                        card_url = ugc.get("jump_url") or ""
                    elif common:
                        card_text = common.get("title") or "网页链接"
                        card_url = common.get("jump_url") or ""
                    elif vote:
                        card_text = f"投票: {vote.get('title') or '投票'}"
                    else:
                        card_text = "链接卡片"
                    if card_url:
                        paragraphs.append(ParaNode(kind="link", text=f"🔗 {card_text}({card_url})"))
                    else:
                        paragraphs.append(ParaNode(kind="link", text=f"📊 {card_text}"))
                # 7 代码块
                elif ptype == 7 and para.get("code"):
                    code = para["code"].get("code_content") or ""
                    if code:
                        paragraphs.append(ParaNode(kind="code", text=f"```\n{code}\n```"))

        # 顶部大图
        if mtype == "MODULE_TYPE_TOP" and module.get("module_top"):
            display = module["module_top"].get("display") or {}
            album = display.get("album") or {}
            for pic in album.get("pics") or []:
                if pic.get("url"):
                    paragraphs.append(ParaNode(kind="image", url=pic["url"]))

    result.paragraphs = paragraphs
    return result


# ==================== 番剧 ====================


@dataclass
class BangumiData:
    ep_id: int
    title: str
    type_name: str
    cover: str
    ep_title: str
    ep_long_title: str
    duration_ms: int
    bvid: str
    cid: int
    rating_score: float | None = None
    rating_count: int | None = None
    update_info: str = ""
    stat: dict[str, Any] = field(default_factory=dict)


def parse_bangumi(result: dict[str, Any], ep_id: int) -> BangumiData:
    """从 pgc/view/web/season 提取指定 ep 信息 (参照 rconsole biliEpInfo)"""
    current: dict[str, Any] | None = None
    for section in [result.get("episodes") or []] + [
        (s.get("episodes") or []) for s in (result.get("section") or [])
    ]:
        for ep in section:
            if int(ep.get("id") or ep.get("ep_id") or 0) == ep_id:
                current = ep
                break
        if current:
            break
    if not current and result.get("main_section"):
        for ep in (result.get("main_section") or {}).get("episodes") or []:
            if int(ep.get("id") or ep.get("ep_id") or 0) == ep_id:
                current = ep
                break
    if not current:
        eps = result.get("episodes") or []
        current = eps[0] if eps else {}

    rating = result.get("rating") or {}
    new_ep = result.get("new_ep") or {}
    return BangumiData(
        ep_id=ep_id,
        title=str(result.get("title") or ""),
        type_name=str(result.get("type_name") or "番剧"),
        cover=str(result.get("cover") or ""),
        ep_title=str(current.get("title") or ""),
        ep_long_title=str(current.get("long_title") or ""),
        duration_ms=int(current.get("duration") or 0),
        bvid=str(current.get("bvid") or ""),
        cid=int(current.get("cid") or 0),
        rating_score=rating.get("score"),
        rating_count=rating.get("count"),
        update_info=str(new_ep.get("desc") or ""),
        stat=result.get("stat") or {},
    )


# ==================== 直播 ====================


@dataclass
class LiveData:
    room_id: int
    title: str
    cover: str
    keyframe: str
    description: str
    tags: str
    area_name: str
    parent_area_name: str
    live_time: str
    uname: str
    face: str
    live_status: int = 0


def parse_live(info: dict[str, Any], room_id: int) -> LiveData:
    """getInfoByRoom 返回结构"""
    room = info.get("room_info") or {}
    anchor = (info.get("anchor_info") or {}).get("base_info") or {}
    return LiveData(
        room_id=room_id,
        title=str(room.get("title") or ""),
        cover=str(room.get("user_cover") or room.get("cover") or ""),
        keyframe=str(room.get("keyframe") or ""),
        description=re.sub(r"</?p>", "", _decode_html(room.get("description") or "")),
        tags=str(room.get("tags") or ""),
        area_name=str(room.get("area_name") or ""),
        parent_area_name=str(room.get("parent_area_name") or ""),
        live_time=str(room.get("live_time") or ""),
        uname=str(anchor.get("uname") or ""),
        face=str(anchor.get("face") or ""),
        live_status=int(room.get("live_status") or 0),
    )


# ==================== 收藏夹 ====================


@dataclass
class FavMedia:
    title: str
    cover: str
    intro: str
    avid: int
    bvid: str

    @property
    def url(self) -> str:
        return f"https://www.bilibili.com/video/{self.bvid}" if self.bvid else f"https://www.bilibili.com/video/av{self.avid}"

    @property
    def desc(self) -> str:
        return f"标题: {self.title}\n简介: {self.intro}\n链接: {self.url}"


@dataclass
class FavData:
    title: str
    cover: str
    intro: str
    ctime: int
    upper_name: str
    upper_face: str
    medias: list[FavMedia] = field(default_factory=list)


def parse_favlist(data: dict[str, Any]) -> FavData:
    info = data.get("info") or {}
    upper = info.get("upper") or {}
    medias: list[FavMedia] = []
    for m in data.get("medias") or []:
        if not m:
            continue
        medias.append(
            FavMedia(
                title=str(m.get("title") or ""),
                cover=str(m.get("cover") or ""),
                intro=str(m.get("intro") or ""),
                avid=int(m.get("id") or 0),
                bvid=str(m.get("bvid") or ""),
            )
        )
    return FavData(
        title=str(info.get("title") or ""),
        cover=str(info.get("cover") or ""),
        intro=str(info.get("intro") or ""),
        ctime=int(info.get("ctime") or 0),
        upper_name=str(upper.get("name") or ""),
        upper_face=str(upper.get("face") or ""),
        medias=medias,
    )


# ==================== 专栏 ====================


@dataclass
class ArticleData:
    cvid: int
    title: str
    summary: str
    author_name: str
    author_face: str
    publish_time: int
    image_urls: list[str] = field(default_factory=list)
    content_text: str = ""


def parse_article(viewinfo: dict[str, Any], view: dict[str, Any] | None, cvid: int) -> ArticleData:
    """x/article/viewinfo + x/article/view 组合提取"""
    images: list[str] = []
    content_text = ""

    if view:
        # content 为 HTML, 简单抽取 img 与文本
        html = view.get("content") or ""
        images = re.findall(r'<img[^>]+src="([^"]+)"', html)
        text = re.sub(r"<img[^>]*>", "\n[图]\n", html)
        text = re.sub(r"<[^>]+>", "", text)
        content_text = _decode_html(text).strip()

    origin_images = viewinfo.get("origin_image_urls") or []
    for u in origin_images:
        if u not in images:
            images.append(u)

    return ArticleData(
        cvid=cvid,
        title=str(viewinfo.get("title") or view.get("title") or ""),
        summary=str(viewinfo.get("summary") or view.get("summary") or ""),
        author_name=str(viewinfo.get("author_name") or ""),
        author_face=str(viewinfo.get("author_face") or (view.get("author") or {}).get("face") or ""),
        publish_time=int(viewinfo.get("publish_time") or view.get("publish_time") or 0),
        image_urls=images,
        content_text=content_text,
    )
