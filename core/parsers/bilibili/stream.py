from typing import Any

from astrbot.api import logger

# 画质 qn -> 目标高度
QN_HEIGHT: dict[int, int] = {
    127: 4320,  # 8K
    126: 2160,  # 杜比视界
    125: 2160,  # HDR
    120: 2160,  # 4K
    116: 1080,  # 1080P60
    112: 1080,  # 1080P+
    80: 1080,   # 1080P
    74: 720,    # 720P60
    64: 720,    # 720P
    32: 480,    # 480P
    16: 360,    # 360P
}

# 画质 qn -> fnval 需要追加的标志位 / fourk
_FNVAL_BASE = 16  # DASH
_FNVAL_AV1 = 2048


def calculate_fnval(qn: int) -> tuple[int, int]:
    """根据画质计算 fnval / fourk (参照 rconsole calculateFnval)"""
    fnval = _FNVAL_BASE | _FNVAL_AV1
    fourk = 0
    if qn >= 127:  # 8K
        fnval |= 1024
        fourk = 1
    elif qn >= 126:  # 杜比
        fnval |= 512
        fourk = 1
    elif qn >= 125:  # HDR
        fnval |= 64
        fourk = 1
    elif qn >= 120:  # 4K
        fnval |= 128
        fourk = 1
    return fnval, fourk


def codec_type_of(codecs: str) -> str:
    c = (codecs or "").lower()
    if "av01" in c or "av1" in c:
        return "av1"
    if "hev1" in c or "hvc1" in c or "hevc" in c:
        return "hevc"
    if "avc1" in c or "avc" in c:
        return "avc"
    return "unknown"


def codec_priority(preferred: str) -> dict[str, int]:
    """编码优先级数字越小越优先"""
    preferred = (preferred or "auto").lower()
    if preferred == "av1":
        return {"av1": 1, "hevc": 2, "avc": 3, "unknown": 999}
    if preferred == "hevc":
        return {"av1": 2, "hevc": 1, "avc": 3, "unknown": 999}
    if preferred == "avc":
        return {"av1": 2, "hevc": 3, "avc": 1, "unknown": 999}
    # auto: av1 > hevc > avc
    return {"av1": 1, "hevc": 2, "avc": 3, "unknown": 999}


def _avoid_slow_cdn(url: str, backup_urls: list[str] | None) -> str:
    """避开 mcdn 等慢速 CDN (参照 rconsole selectAndAvoidMCdnUrl 简化版)"""
    slow_markers = (".mcdn.bilivideo.cn", "mountaintoys.cn", ".szbdyd.com")

    def is_slow(u: str) -> bool:
        return any(m in u for m in slow_markers)

    def is_fast(u: str) -> bool:
        return ("upos-sz-mirror" in u) or ("upos-hz-mirror" in u) or (".bilivideo.com" in u and not is_slow(u))

    if not is_slow(url):
        return url
    for u in backup_urls or []:
        if is_fast(u):
            return u
    for u in backup_urls or []:
        if not is_slow(u):
            return u
    return url


def select_streams(
    dash: dict[str, Any],
    *,
    qn: int,
    preferred_codec: str = "auto",
) -> tuple[str, str | None]:
    """从 dash 数据中选出 (视频URL, 音频URL|None)"""
    videos: list[dict[str, Any]] = dash.get("video") or []
    audios: list[dict[str, Any]] = dash.get("audio") or []
    if not videos:
        raise ValueError("dash 数据中没有视频流")

    target_height = QN_HEIGHT.get(qn)
    if target_height is None:
        heights = [v.get("height") or 0 for v in videos]
        target_height = max(heights) if heights else 0
        logger.warning(f"[BiliExt] 未知 qn={qn}, 使用最高可用分辨率 {target_height}p")

    matching = [v for v in videos if (v.get("height") or 0) == target_height]
    if not matching:
        # 找不超过目标高度的最高档
        lower = sorted(
            (v for v in videos if (v.get("height") or 0) <= target_height),
            key=lambda v: -(v.get("height") or 0),
        )
        if lower:
            top = lower[0].get("height") or 0
            matching = [v for v in lower if (v.get("height") or 0) == top]
        if not matching:
            # 全部高于目标, 用最低档
            min_h = min((v.get("height") or 0) for v in videos)
            matching = [v for v in videos if (v.get("height") or 0) == min_h]
            logger.warning(f"[BiliExt] 请求画质不可用, 降级到最低 {min_h}p")

    prio = codec_priority(preferred_codec)
    matching.sort(
        key=lambda v: (
            prio.get(codec_type_of(v.get("codecs", "")), 999),
            v.get("bandwidth") or 0,
        )
    )
    chosen = matching[0]
    logger.debug(
        f"[BiliExt] 选中视频流: {chosen.get('height')}p "
        f"{codec_type_of(chosen.get('codecs', ''))}({chosen.get('codecs')}) "
        f"{round((chosen.get('bandwidth') or 0) / 1024)}kbps"
    )

    v_url = _avoid_slow_cdn(
        chosen.get("baseUrl") or chosen.get("base_url") or "",
        list(chosen.get("backupUrl") or chosen.get("backup_url") or []),
    )
    if not v_url:
        raise ValueError("选中的视频流没有下载地址")

    a_url = None
    if audios:
        # 取码率最高的音频
        best_a = max(audios, key=lambda a: a.get("bandwidth") or 0)
        a_url = _avoid_slow_cdn(
            best_a.get("baseUrl") or best_a.get("base_url") or "",
            list(best_a.get("backupUrl") or best_a.get("backup_url") or []),
        )

    return v_url, a_url


def pick_durl(durls: list[dict[str, Any]]) -> str:
    """从 durl 列表取第一条完整流的 URL"""
    if not durls:
        raise ValueError("durl 为空")
    first = durls[0]
    return _avoid_slow_cdn(
        first.get("url") or "",
        list(first.get("backup_url") or []),
    )
