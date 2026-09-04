# main.py

import asyncio
import re
from typing import Any, TypeVar, cast

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import Image
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.star.star import star_registry

from .core.config import PluginConfig
from .core.parsers import (
    BaseParser,
    JMComicParser,
    MagnetParser,
    NCMParser,
    TelegramParser,
    XHSParser,
)

T = TypeVar("T", bound=BaseParser)

PARSER_CLASSES: dict[str, type[BaseParser]] = {
    "telegram": TelegramParser,
    "magnet": MagnetParser,
    "jmcomic": JMComicParser,
    "ncm": NCMParser,
    "xhs": XHSParser,
}

MISSING_DEP_MSG = "本插件强依赖 astrbot_plugin_parser，未检测到依赖插件，请先在插件市场安装！"


class ParserPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.raw_config = config
        self.cfg = PluginConfig(config, context=context)
        self.is_ready = False
        self.last_base_star: Any = None
        self.injected_parsers: dict[str, BaseParser] = {}
        self._watcher_task: asyncio.Task | None = None

    async def initialize(self):
        """插件加载时执行注入"""
        base_star = self._find_base_star()
        if base_star:
            self._inject_parsers(base_star)
        else:
            logger.error("[ParserExt] 未检测到基础插件 astrbot_plugin_parser，本插件强依赖原插件。请先在插件市场安装原插件！")
            self.is_ready = False

        # 启动轻量守望协程（监听原插件是否加载或热重载）
        self._watcher_task = asyncio.create_task(self._watch_base_star())

    async def terminate(self):
        """插件卸载时取消任务并释放资源"""
        if self._watcher_task and not self._watcher_task.done():
            self._watcher_task.cancel()

        # 关闭所有注入解析器的 session
        for parser in self.injected_parsers.values():
            await parser.close_session()
        self.injected_parsers.clear()
        self.is_ready = False

    def _find_base_star(self) -> Any:
        """从 AstrBot star_registry 中寻找 astrbot_plugin_parser 实例"""
        for meta in star_registry:
            if not meta.activated:
                continue
            if meta.name == "astrbot_plugin_parser" or meta.root_dir_name == "astrbot_plugin_parser":
                if meta.star_cls and hasattr(meta.star_cls, "parser_map") and hasattr(meta.star_cls, "key_pattern_list"):
                    return meta.star_cls
        return None

    def _inject_parsers(self, base_star: Any):
        """将当前启用的扩展解析器注入到 base_star 中"""
        try:
            # 关联原插件配置
            self.cfg.base_config = getattr(base_star, "cfg", None)

            # 获取当前启用的扩展平台
            enabled_platforms = set(self.cfg.parser.enabled_platforms())

            # 实例化启用的解析器
            new_injected: dict[str, BaseParser] = {}
            for name, cls in PARSER_CLASSES.items():
                if name in enabled_platforms:
                    parser_inst = cls(self.cfg, base_star.downloader)
                    new_injected[name] = parser_inst

            # 释放旧注入实例的 session
            for old_p in self.injected_parsers.values():
                asyncio.create_task(old_p.close_session())
            self.injected_parsers = new_injected

            # 注入到 base_star.parser_map
            my_keywords: set[str] = set()
            for parser in self.injected_parsers.values():
                for kw, _ in parser._key_patterns:
                    my_keywords.add(kw)
                    base_star.parser_map[kw] = parser

            # 从 base_star.key_pattern_list 剔除需要覆盖的同名关键词，避免重复冲突
            base_star.key_pattern_list = [
                item for item in base_star.key_pattern_list if item[0] not in my_keywords
            ]

            # 将扩展解析器的正则追加到 base_star.key_pattern_list
            for parser in self.injected_parsers.values():
                for kw, pat in parser._key_patterns:
                    compiled = re.compile(pat) if isinstance(pat, str) else pat
                    base_star.key_pattern_list.append((kw, compiled))

            # 重新按关键词长度降序排序，保证长关键词优先匹配
            base_star.key_pattern_list.sort(key=lambda x: -len(x[0]))

            self.last_base_star = base_star
            self.is_ready = True
            enabled_names = list(self.injected_parsers.keys())
            logger.info(
                f"[ParserExt] 成功将扩展解析器注入到 astrbot_plugin_parser: {'、'.join(enabled_names) if enabled_names else '无'}"
            )
        except Exception as e:
            logger.error(f"[ParserExt] 注入解析器失败: {e}", exc_info=True)
            self.is_ready = False

    async def _watch_base_star(self):
        """轻量守望协程：每 3 秒检测原插件状态"""
        while True:
            try:
                await asyncio.sleep(3)
                curr_base = self._find_base_star()
                if curr_base and curr_base is not self.last_base_star:
                    logger.info("[ParserExt] 检测到原插件 astrbot_plugin_parser 实例发生变更（重载），正在自动重新注入...")
                    self._inject_parsers(curr_base)
                elif not curr_base and self.is_ready:
                    logger.warning("[ParserExt] 基础插件 astrbot_plugin_parser 已失效或未加载")
                    self.is_ready = False
                    self.last_base_star = None
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[ParserExt] 守望任务异常: {e}")

    def _get_parser_by_type(self, parser_type: type[T]) -> T:
        for parser in self.injected_parsers.values():
            if isinstance(parser, parser_type):
                return cast(T, parser)
        raise ValueError(f"未找到类型为 {parser_type} 的 parser 实例")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ext重载", alias={"ext_reload"})
    async def reload_ext(self, event: AstrMessageEvent):
        """手动重新注入扩展解析器"""
        curr_base = self._find_base_star()
        if not curr_base:
            yield event.plain_result(MISSING_DEP_MSG)
            return
        self._inject_parsers(curr_base)
        yield event.plain_result(
            f"扩展解析器重新注入成功！已挂载平台：{'、'.join(self.injected_parsers.keys())}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("登录网易云", alias={"nlogin", "wylogin"})
    async def login_ncm(self, event: AstrMessageEvent):
        """扫码登录网易云音乐"""
        if not self.is_ready:
            yield event.plain_result(MISSING_DEP_MSG)
            return
        try:
            parser: NCMParser = self._get_parser_by_type(NCMParser)
        except ValueError:
            yield event.plain_result("网易云扩展解析器未启用，请在插件配置中开启。")
            return

        qrcode = await parser.login_with_qrcode()
        yield event.chain_result([Image.fromBytes(qrcode)])
        async for msg in parser.check_qr_state():
            yield event.plain_result(msg)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("登录Telegram", alias={"tglogin", "登录tg"})
    async def login_telegram(
        self,
        event: AstrMessageEvent,
        args: GreedyStr,
    ):
        """登录 Telegram(扫码) 或完成 2FA: tglogin 2fa <密码>"""
        if not self.is_ready:
            yield event.plain_result(MISSING_DEP_MSG)
            return
        try:
            parser: TelegramParser = self._get_parser_by_type(TelegramParser)
        except ValueError:
            yield event.plain_result("Telegram 扩展解析器未启用，请在插件配置中开启。")
            return

        if parser.login is None:
            yield event.plain_result(
                "Telegram 解析器未就绪，可能依赖安装失败，请检查日志"
            )
            return
        args_str = str(args).strip()

        # 分支一: 2FA 子命令 (tglogin 2fa <密码>)
        if args_str == "2fa" or args_str.startswith("2fa "):
            password = args_str[3:].strip()
            if not password:
                yield event.plain_result("请提供 2FA 密码,用法: tglogin 2fa <密码>")
                return
            result = await parser.login.complete_2fa(password)
            yield event.plain_result(result)
            return

        # 分支二: 正常扫码登录
        try:
            qr_png = await parser.login.start_qr_login()
        except Exception as e:
            yield event.plain_result(f"生成二维码失败: {e}")
            return
        yield event.chain_result([Image.fromBytes(qr_png)])
        async for msg in parser.login.wait_qr_login():
            yield event.plain_result(msg)