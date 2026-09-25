"""哔哩哔哩凭证管理"""

import asyncio
import io
import json
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

import qrcode
from aiohttp import ClientError

from astrbot.api import logger

from data.plugins.astrbot_plugin_parser.core.exception import ParseException

from . import endpoints

if TYPE_CHECKING:
    from . import BilibiliParser

# 扫码轮询返回码
_QR_SUCCESS = 0
_QR_EXPIRED = 86038
_QR_CONFIRM = 86090


def extract_sessdata(cookie_or_sessdata: str) -> str:
    """从 cookie 字符串或裸 SESSDATA 中提取 SESSDATA 值"""
    if not cookie_or_sessdata:
        return ""
    if "SESSDATA=" in cookie_or_sessdata:
        for part in cookie_or_sessdata.split(";"):
            part = part.strip()
            if part.startswith("SESSDATA="):
                return part[len("SESSDATA="):]
        return ""
    return cookie_or_sessdata.strip()


def cookie_header_from(raw: str) -> str:
    """构造请求用的 Cookie 头: 完整 cookie 原样发送, 否则只带 SESSDATA"""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if "SESSDATA=" in raw:
        return raw
    return f"SESSDATA={raw}"


class BiliCredential:
    """SESSDATA 凭证: 配置读取 / 文件缓存 / 扫码登录"""

    def __init__(self, parser: "BilibiliParser"):
        self.parser = parser
        self.credential_file = parser.cfg.data_dir / "cookies" / "bilibili_sessdata.txt"
        self._sessdata: str = ""
        self._qr_key: str | None = None

    # ---------- 读写 ----------

    @property
    def sessdata(self) -> str:
        if self._sessdata:
            return self._sessdata
        self._load()
        return self._sessdata

    @property
    def cookie_header(self) -> str:
        return cookie_header_from(self.sessdata)

    def _load(self) -> None:
        """优先取配置 cookies, 其次取缓存文件"""
        raw = (getattr(self.parser.mycfg, "cookies", None) or "").strip()
        sess = extract_sessdata(raw)
        if sess:
            self._sessdata = raw if "SESSDATA=" in raw else sess
            self._save()
            return
        if self.credential_file.exists():
            try:
                data = json.loads(self.credential_file.read_text(encoding="utf-8"))
                sess = extract_sessdata(data.get("sessdata") or data.get("SESSDATA") or "")
                if sess:
                    self._sessdata = sess
            except Exception as e:
                logger.warning(f"[BiliExt] 读取 SESSDATA 缓存失败: {e}")

    def _save(self) -> None:
        if not self._sessdata:
            return
        try:
            self.credential_file.parent.mkdir(parents=True, exist_ok=True)
            self.credential_file.write_text(
                json.dumps({"sessdata": extract_sessdata(self._sessdata)}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[BiliExt] 保存 SESSDATA 失败: {e}")

    def set_sessdata(self, value: str) -> None:
        self._sessdata = value.strip()
        self._save()
        self.parser.headers["cookie"] = self.cookie_header

    def clear(self) -> None:
        self._sessdata = ""
        if self.credential_file.exists():
            try:
                self.credential_file.unlink()
            except Exception:
                pass

    # ---------- 登录态检测 ----------

    async def check_login(self) -> dict | None:
        """请求 nav 接口检测登录态, 返回用户信息; 未登录返回 None"""
        try:
            async with self.parser.session.get(
                endpoints.NAV,
                headers=self.parser.bili_headers(),
            ) as resp:
                data = await resp.json(content_type=None)
        except (ClientError, asyncio.TimeoutError) as e:
            raise ParseException(f"检查 B 站登录态失败: {e}") from e
        payload = data.get("data") or {}
        if data.get("code") != 0 or not payload.get("isLogin"):
            return None
        return payload

    async def get_wbi_keys(self) -> tuple[str, str]:
        """获取最新 img_key / sub_key (无登录态也可用)"""
        try:
            async with self.parser.session.get(
                endpoints.NAV,
                headers=self.parser.bili_headers(),
            ) as resp:
                data = await resp.json(content_type=None)
        except (ClientError, asyncio.TimeoutError) as e:
            raise ParseException(f"获取 WBI key 失败: {e}") from e
        wbi_img = ((data.get("data") or {}).get("wbi_img")) or {}
        img_url, sub_url = wbi_img.get("img_url"), wbi_img.get("sub_url")
        if not img_url or not sub_url:
            raise ParseException("nav 接口未返回 wbi_img")
        from .wbi import parse_wbi_keys

        return parse_wbi_keys(img_url, sub_url)

    async def sign_params(self, params: dict) -> dict:
        """WBI 签名请求参数"""
        from .wbi import enc_wbi

        img_key, sub_key = await self.get_wbi_keys()
        return enc_wbi(params, img_key, sub_key)

    # ---------- 扫码登录 (rconsole passport 接口, 只保留 SESSDATA) ----------

    async def login_with_qrcode(self) -> bytes:
        """生成登录二维码, 返回 PNG bytes"""
        try:
            async with self.parser.session.get(
                endpoints.QR_GENERATE,
                headers=self.parser.bili_headers(),
            ) as resp:
                data = await resp.json(content_type=None)
        except (ClientError, asyncio.TimeoutError) as e:
            raise ParseException(f"获取 B 站登录二维码失败: {e}") from e

        if data.get("code") != 0:
            raise ParseException(f"获取 B 站登录二维码失败: {data.get('message')}")
        payload = data.get("data") or {}
        url = payload.get("url")
        self._qr_key = payload.get("qrcode_key")
        if not url or not self._qr_key:
            raise ParseException("B 站登录二维码接口返回异常")

        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")  # type: ignore[attr-defined]
        return buf.getvalue()

    async def check_qr_state(self) -> AsyncGenerator[str, None]:
        """轮询扫码登录状态"""
        qr_key = self._qr_key
        if not qr_key:
            yield "未找到二维码 key, 请重新生成"
            return

        scan_tip_pending = True
        for _ in range(30):
            if self._qr_key != qr_key:
                return
            try:
                async with self.parser.session.get(
                    endpoints.QR_POLL.format(qrcode_key=qr_key),
                    headers=self.parser.bili_headers(),
                ) as resp:
                    data = await resp.json(content_type=None)
            except Exception:
                await asyncio.sleep(2)
                continue

            payload = data.get("data") or {}
            code = payload.get("code")
            if code == _QR_SUCCESS:
                # 只保留 SESSDATA, 不保存/不刷新 refresh_token
                url = payload.get("url") or ""
                sess = ""
                if "SESSDATA=" in url:
                    from urllib.parse import parse_qs, urlparse

                    qs = parse_qs(urlparse(url).query)
                    sess = (qs.get("SESSDATA") or [""])[0]
                if not sess:
                    # 兜底: 从 set-cookie 提取
                    for header_val in resp.headers.getall("Set-Cookie", []):
                        if "SESSDATA=" in header_val:
                            sess = extract_sessdata(header_val)
                            if sess:
                                break
                if not sess:
                    yield "登录成功, 但未能提取 SESSDATA, 请重新扫码"
                    return
                self.set_sessdata(sess)
                yield "登录成功, SESSDATA 已保存"
                return
            if code == _QR_CONFIRM:
                if scan_tip_pending:
                    yield "二维码已扫描, 请在手机上确认登录"
                    scan_tip_pending = False
            elif code == _QR_EXPIRED:
                yield "二维码已过期, 请重新生成"
                return
            await asyncio.sleep(2)
        else:
            yield "二维码登录超时, 请重新生成"
