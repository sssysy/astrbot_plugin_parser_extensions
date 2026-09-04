<div align="center">

<img src="https://raw.githubusercontent.com/sssysy/astrbot_plugin_parser_extensions/main/logo.gif" width="250" />

# astrbot_plugin_parser_extensions

_✨ 万能解析器 (扩展包) ✨_  

[![License](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-3.4%2B-orange.svg)](https://github.com/Soulter/AstrBot)
[![GitHub](https://img.shields.io/badge/前置插件-astrbot__plugin__parser-blue)](https://github.com/Zhalslar/astrbot_plugin_parser)
[![GitHub](https://img.shields.io/badge/追加-sssysy-cyan)](https://github.com/sssysy)

</div>

> [!CAUTION]
> 本插件为 [zhalslar/astrbot_plugin_parser](https://github.com/Zhalslar/astrbot_plugin_parser) 的**动态扩展包**，**强依赖原插件运行**。请先在 AstrBot 插件市场安装并启用原插件！

---

## 追加解析器

本扩展包在原插件基础上，采用**动态注入方式**为原插件追加或增强以下平台的解析能力：

| 平台       | 触发的消息形态                    | 视频 | 图集 | 音频 |
| ---------- | --------------------------------- | ---- | ---- | ---- |
| Telegram   | 链接(频道消息/群组消息)            | ✅​  | ✅​  | ✅​  |
| 磁力链接   | 链接(magnet:?)                    | ❌️  | ✅​  | ❌️  |
| JMComic    | 链接(含短链) / jm 号               | ❌️  | ✅​  | ❌️  |
| 网易云音乐 (修复) | 链接(单曲链接/短链)               | ❌️  | ✅​  | ✅​  |
| 小红书 (增强)  | 链接(含短链)/卡片                 | ✅​  | ✅​  | ❌️  |

---

## ⚙️ 配置说明

在 AstrBot 插件管理面板中配置本插件：
- **扩展解析器列表**：通过追加插件设置来添加追加解析器
- 全局代理、白名单、黑名单、防抖秒数等全局通用设置直接在原插件（astrbot_plugin_parser）面板配置即可，本插件自动遵从。

---

## 🎉 专属指令

| 指令 | 别名 | 权限 | 说明 |
| :---: | :---: | :---: | :---: |
| `登录网易云` | `nlogin`, `wylogin` | ADMIN | 弹出二维码扫码登录网易云音乐 |
| `登录Telegram` | `tglogin`, `登录tg` | ADMIN | 弹出二维码扫码登录 Telegram；支持 `tglogin 2fa <密码>` |
| `ext重载` | `ext_reload` | ADMIN | 手动触发重新注入扩展解析器到原插件中 |

> `开启解析`、`关闭解析`、`登录B站` 等通用指令请直接使用原插件自带的官方指令。

---

## 🎉 致谢

- [Zhalslar/astrbot_plugin_parser](https://github.com/Zhalslar/astrbot_plugin_parser)
- [NeteaseCloudMusicApiEnhanced](https://github.com/neteasecloudmusicapienhanced/api-enhanced)