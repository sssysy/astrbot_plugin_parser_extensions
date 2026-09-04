from data.plugins.astrbot_plugin_parser.core.parsers.base import BaseParser
from .jmcomic import JMComicParser
from .kugou import KuGouParser
from .magnet import MagnetParser
from .ncm import NCMParser
from .telegram import TelegramParser
from .xhs import XHSParser

__all__ = [
    "BaseParser",
    "JMComicParser",
    "KuGouParser",
    "MagnetParser",
    "NCMParser",
    "TelegramParser",
    "XHSParser",
]

