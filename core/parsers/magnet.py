from pathlib import Path
from re import Match
from typing import ClassVar

from PIL import Image, ImageFilter
from aiohttp import ClientError

from ..config import PluginConfig
from ..data import ParseResult, Platform, TextContent, ImageContent
from ..download import Downloader
from ..exception import ParseException
from .base import BaseParser, handle


class MagnetUtils:
    """磁力链接解析工具类"""

    @staticmethod
    def gaussian_blur(image_path: str | Path, output_path: str | Path | None = None, radius: int = 15) -> Path:
        """对图片施加全局高斯模糊

        Args:
            image_path: 输入图片路径
            output_path: 输出图片路径，为 None 时覆盖原图
            radius: 模糊半径
        """
        image_path = Path(image_path)
        if output_path is None:
            output_path = image_path
        else:
            output_path = Path(output_path)
        img = Image.open(image_path)
        blurred = img.filter(ImageFilter.GaussianBlur(radius=radius))
        blurred.save(output_path)
        return output_path

    @staticmethod
    def fmt_size(size_bytes: int | float) -> str:
        """将字节数转换为可读的文件大小字符串

        Args:
            size_bytes: 文件大小（字节）

        Returns:
            可读的大小字符串，如 "1.23 MB"
        """
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size_bytes < 1024:
                return f"{size_bytes:.2f} {unit}"
            size_bytes /= 1024
        return f"{size_bytes:.2f} PB"


class MagnetParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="magnet", display_name="磁力链接")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.magnet

    async def _parseprocess(self, magnet: str) -> dict:
        # 请求磁力链接数据
        url = "https://whatslink.info/api/v1/link"
        try:
            async with self.session.get(url, params={"url": magnet}) as resp:
                if resp.status >= 400:
                    raise ParseException(f"磁力解析失败: HTTP {resp.status}")
                return await resp.json()
        except ClientError as e:
            raise ParseException(f"磁力解析请求失败: {e}") from e

    async def send_with_blur(self, img_urls: list[str], text: str) -> ParseResult:
        """下载图片并施加高斯模糊后发送"""
        # 下载图片
        blurred_paths: list[Path] = []
        for img_url in img_urls:
            img_path = await self.downloader.download_img(
                url=img_url, headers=self.headers, proxy=self.proxy
            )
            #增加高斯模糊
            MagnetUtils.gaussian_blur(img_path, Path(f"{img_path}_blur.jpg"))
            blurred_paths.append(Path(f"{img_path}_blur.jpg"))
        return self.result(
            contents = [
                TextContent(text),
                *[ImageContent(p) for p in blurred_paths]
                ]
        )

    async def send_with_original(self, img_urls: list[str], text: str) -> ParseResult:
        """下载图片直接发送"""
        return self.result(
            contents = [
                TextContent(text),
                *self.create_image_contents(img_urls) # 暂时只发第一张预览图，后面再写
                ]
        )

    async def send_without_img(self, text: str) -> ParseResult:
        """发送文本"""
        return self.result(
            contents = [
                TextContent(text)
                ]
        )

    @handle("magnet", r"magnet:\?[^\s]+")
    async def _parse_magnet(self, searched: Match[str]):
        magnet = searched.group(0)
        data = await self._parseprocess(magnet)

        # 解析磁力链接数据
        file_type = data.get("file_type", "")
        title = data.get("name", "")
        size = data.get("size", 0)
        size_convert = MagnetUtils.fmt_size(size)
        
        #解析预览图片列表
        preview_json = data.get("screenshots", [])
        img_urls = [
            p.get("screenshot") for p in preview_json if p.get("screenshot")
        ]
        send_text = f"标题：{title}\n\n磁链类型：{file_type}\n\n文件大小：{size_convert}"
        send_type = self.mycfg.image_send_mode
        if img_urls and send_type == "blur":
            return await self.send_with_blur(img_urls, send_text)
        elif img_urls and send_type == "original":
            return await self.send_with_original(img_urls, send_text)
        else:
            return await self.send_without_img(send_text)
        
        
