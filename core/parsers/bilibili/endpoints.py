"""哔哩哔哩 API 端点"""

# 视频信息 https://github.com/SocialSisterYi/bilibili-API-collect/blob/master/docs/video/info.md
VIDEO_INFO = "https://api.bilibili.com/x/web-interface/view"

# BVID -> 分P CID 列表
BVID_PAGELIST = "https://api.bilibili.com/x/player/pagelist?bvid={bvid}&jsonp=jsonp"

# 视频播放流 (WBI) https://github.com/SocialSisterYi/bilibili-API-collect/blob/master/docs/video/videostream_url.md
PLAY_STREAM = (
    "https://api.bilibili.com/x/player/wbi/playurl"
    "?cid={cid}&bvid={bvid}&qn={qn}&fnval={fnval}&fourk={fourk}"
)

# AI 总结
AI_CONCLUSION = "https://api.bilibili.com/x/web-interface/view/conclusion/get"

# 在线人数
ONLINE_TOTAL = "https://api.bilibili.com/x/player/online/total?bvid={bvid}&cid={cid}"

# 番剧信息 https://github.com/SocialSisterYi/bilibili-API-collect/blob/master/docs/bangumi/info.md
EP_INFO = "https://api.bilibili.com/pgc/view/web/season?ep_id={ep_id}"
SS_INFO = "https://api.bilibili.com/pgc/web/season/section?season_id={ss_id}"

# 番剧播放流 https://github.com/SocialSisterYi/bilibili-API-collect/blob/master/docs/bangumi/videostream_url.md
BANGUMI_STREAM = (
    "https://api.bilibili.com/pgc/player/web/playurl"
    "?ep_id={ep_id}&cid={cid}&qn={qn}&fnval={fnval}&fourk={fourk}"
)

# 动态/图文详情 https://github.com/SocialSisterYi/bilibili-API-collect/blob/master/docs/opus/detail.md
OPUS_DETAIL = (
    "https://api.bilibili.com/x/polymer/web-dynamic/v1/opus/detail"
    "?id={opus_id}&features=onlyfansVote,onlyfansAssetsV2,decorationCard,"
    "htmlNewStyle,ugcDelete,editable,opusPrivateVisible,tribeeEdit,"
    "avatarAutoTheme,avatarTypeOpus"
)

# 专栏信息 https://github.com/SocialSisterYi/bilibili-API-collect/blob/master/docs/article/info.md
ARTICLE_INFO = "https://api.bilibili.com/x/article/viewinfo?id={cvid}"
ARTICLE_VIEW = "https://api.bilibili.com/x/article/view?id={cvid}"

# 直播间信息 https://github.com/SocialSisterYi/bilibili-API-collect/blob/master/docs/live/info.md
LIVE_ROOM_INFO = "https://api.live.bilibili.com/room/v1/Room/get_info?room_id={room_id}"
LIVE_ROOM_FULL = (
    "https://api.live.bilibili.com/xlive/web-room/v1/index/getInfoByRoom?room_id={room_id}"
)
LIVE_STREAM_URL = "https://api.live.bilibili.com/room/v1/Room/playUrl?cid={room_id}&platform=web&qn=10000"

# 收藏夹内容 https://github.com/SocialSisterYi/bilibili-API-collect/blob/master/docs/fav/list.md
FAV_CONTENT = (
    "https://api.bilibili.com/x/v3/fav/resource/list"
    "?media_id={fid}&pn=1&ps=20&order=mtime&type=0"
)

# 导航栏用户信息 (WBI key 来源 / 登录态检测) https://github.com/SocialSisterYi/bilibili-API-collect/blob/master/docs/login/login_info.md
NAV = "https://api.bilibili.com/x/web-interface/nav"

# 扫码登录 https://github.com/SocialSisterYi/bilibili-API-collect/blob/master/docs/login/login_action/QR.md
QR_GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key={qrcode_key}"
