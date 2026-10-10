from __future__ import annotations

import json
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.context import Context
from astrbot.core.utils.astrbot_path import (
    get_astrbot_plugin_data_path,
    get_astrbot_plugin_path,
)
from data.plugins.astrbot_plugin_parser.core.config import (
    ConfigNode,
    ConfigNodeContainer,
)


class ParserItem(ConfigNode):
    __template_key: str
    enable: bool
    use_proxy: bool | None
    cookies: str | None
    base_url: str | None
    quality: str | None
    image_send_mode: str | None
    nsfw: str | None
    max_page: int | None
    api_id: int | None
    api_hash: str | None
    parse_original_image: bool | None
    auto_claim_vip: bool | None
    video_quality: str | None
    video_codec: str | None

    @property
    def name(self) -> str:
        return self._data.get("__template_key", "")


class ParserConfig(ConfigNodeContainer):
    telegram: ParserItem
    magnet: ParserItem
    jmcomic: ParserItem
    ncm: ParserItem
    xhs: ParserItem
    kugou: ParserItem

    def __init__(self, nodes: list[dict[str, Any]]):
        super().__init__(nodes, item_cls=ParserItem)

    def platforms(self) -> list[str]:
        return list(self._nodes.keys())

    def enabled_platforms(self) -> list[str]:
        return [k for k, v in self._nodes.items() if getattr(v, "enable", True)]


class PluginConfig(ConfigNode):
    parsers_template: list[dict[str, Any]]

    _plugin_name = "astrbot_plugin_parser_extensions"

    def __init__(
        self,
        config: AstrBotConfig,
        context: Context,
        base_config: Any = None,
    ):
        self.plugin_dir = Path(__file__).resolve().parent.parent
        self.default_template_file = self.plugin_dir / "default_template.json"

        if isinstance(config, MutableMapping) and "parsers_template" not in config:
            config["parsers_template"] = []

        super().__init__(config)
        self.context = context
        self.base_config = base_config

        self.local_data_dir = Path(get_astrbot_plugin_data_path()) / self._plugin_name
        self.local_cache_dir = self.local_data_dir / "cache"
        self.local_cache_dir.mkdir(parents=True, exist_ok=True)
        self.cookie_dir = self.local_data_dir / "cookies"
        self.cookie_dir.mkdir(parents=True, exist_ok=True)

        # ---------- Parser ----------
        self._sync_parser_templates()

        self.parser = ParserConfig(self.parsers_template)

    @property
    def _template_state_file(self) -> Path:
        """Path of the sidecar file tracking parser keys ever offered to this config."""
        return self.local_data_dir / "parsers_state.json"

    def _sync_parser_templates(self) -> None:
        """Sync the saved parser templates with the bundled default template.

        Only templates that have never been seen before are appended, so a parser
        deleted by the user is not re-added on restart, while parsers introduced by
        a plugin upgrade are still picked up automatically.
        """
        defaults = self.load_parser_template(self.default_template_file)
        default_keys = [
            node.get("__template_key")
            for node in defaults
            if node.get("__template_key")
        ]

        templates = self.parsers_template
        if not isinstance(templates, list):
            templates = []
            self.parsers_template = templates

        existing = {
            node.get("__template_key")
            for node in templates
            if isinstance(node, dict) and node.get("__template_key")
        }

        known = self._load_template_state()
        if known is None:
            # First run with state tracking: treat both the saved config and the
            # bundled template as already known so earlier deletions are respected.
            known = existing | set(default_keys)
            if not templates:
                # Empty config (fresh install): seed it from the default template.
                templates.extend(defaults)
                existing |= set(default_keys)
                self._persist()
            self._save_template_state(known)
            return

        # Upgrade path: only append parsers that were never seen before.
        added = [k for k in default_keys if k not in known and k not in existing]
        if not added:
            return

        for node in defaults:
            if node.get("__template_key") in added:
                templates.append(node)
        known |= set(default_keys) | existing
        self._save_template_state(known)
        self._persist()
        logger.info(f"[ParserExt] 已补充新增解析器配置: {'、'.join(added)}")

    def _load_template_state(self) -> set[str] | None:
        """Load the set of parser keys already offered to this config.

        Returns:
            The recorded keys, or None when no state has been recorded yet.
        """
        try:
            with self._template_state_file.open(encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return None
        except Exception as e:
            logger.warning(f"[ParserExt] 读取解析器状态失败，按首次运行处理: {e}")
            return None
        keys = data.get("known_keys") if isinstance(data, dict) else None
        return {str(k) for k in keys} if isinstance(keys, list) else None

    def _save_template_state(self, known: set[str]) -> None:
        """Persist the set of parser keys already offered to this config.

        Args:
            known: The parser keys to remember.
        """
        try:
            with self._template_state_file.open("w", encoding="utf-8") as f:
                json.dump(
                    {"known_keys": sorted(known)}, f, ensure_ascii=False, indent=2
                )
        except Exception as e:
            logger.warning(f"[ParserExt] 保存解析器状态失败: {e}")

    def _persist(self) -> None:
        """Best-effort save of the underlying config to disk."""
        try:
            self.save_config()
        except Exception:
            pass

    @property
    def data_dir(self) -> Path:
        return self.local_data_dir

    @property
    def cache_dir(self) -> Path:
        if self.base_config and hasattr(self.base_config, "cache_dir"):
            return self.base_config.cache_dir
        return self.local_cache_dir

    @property
    def proxy(self) -> str | None:
        if self.base_config and hasattr(self.base_config, "proxy"):
            return getattr(self.base_config, "proxy", None)
        return None

    @property
    def common_timeout(self) -> int:
        if self.base_config and hasattr(self.base_config, "common_timeout"):
            return getattr(self.base_config, "common_timeout", 15)
        return 15

    @property
    def download_retry_times(self) -> int:
        if self.base_config and hasattr(self.base_config, "download_retry_times"):
            return getattr(self.base_config, "download_retry_times", 2)
        return 2

    @property
    def max_size(self) -> int:
        if self.base_config and hasattr(self.base_config, "max_size"):
            return getattr(self.base_config, "max_size", 90 * 1024 * 1024)
        return 90 * 1024 * 1024

    @property
    def show_download_fail_tip(self) -> bool:
        if self.base_config and hasattr(self.base_config, "show_download_fail_tip"):
            return bool(getattr(self.base_config, "show_download_fail_tip", True))
        return True

    @property
    def single_heavy_render_card(self) -> bool:
        if self.base_config and hasattr(self.base_config, "single_heavy_render_card"):
            return bool(getattr(self.base_config, "single_heavy_render_card", False))
        return False

    @staticmethod
    def load_parser_template(file: Path) -> list[dict[str, Any]]:
        try:
            with file.open(encoding="utf-8-sig") as f:
                template = json.loads(f.read())
                logger.info(f"[ParserExt] 加载模板成功: {file}")
                return template
        except Exception as e:
            logger.error(f"[ParserExt] 加载模板失败: {e}")
            return []
