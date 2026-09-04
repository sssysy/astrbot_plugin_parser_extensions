<div align="center">

<img src="https://raw.githubusercontent.com/sssysy/astrbot_plugin_parser_extensions/main/logo.gif" width="300" />

# astrbot_plugin_parser_extensions

_✨ 万能解析器 (扩展包) ✨_  

[![License](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-3.4%2B-orange.svg)](https://github.com/Soulter/AstrBot)
[![GitHub](https://img.shields.io/badge/基础插件-astrbot__plugin__parser-blue)](https://github.com/Zhalslar/astrbot_plugin_parser)
[![GitHub](https://img.shields.io/badge/扩展维护-sssysy-cyan)](https://github.com/sssysy)

</div>

> ⚠️ **重要提示**：本插件为 [zhalslar/astrbot_plugin_parser](https://github.com/Zhalslar/astrbot_plugin_parser) 的**动态扩展包**，**强依赖原插件运行**。请先在 AstrBot 插件市场安装并启用原插件！

---

## 📖 扩展能力

本扩展包在原插件基础上，采用**动态注入架构**为原插件追加或增强以下平台的解析能力：

| 平台 | 类型 | 特性说明 |
| --- | --- | --- |
| **Telegram** | 纯新增追加 | 支持频道消息、群组消息解析，支持图片/视频/音频/文件流式下载，支持扫码登录与 2FA 验证 |
| **磁力链接** | 纯新增追加 | 支持 `magnet:?` 链接解析、种子信息提取与封面预览（支持打码/原图发送） |
| **JMComic** | 纯新增追加 | 禁漫天堂漫画解析，支持封面模糊防封、正文自动打包生成 PDF 发送 |
| **网易云音乐** | 深度覆盖增强 | 基于 NodeJS API，支持扫码登录、极高/无损/Hi-Res 真实音质下载、歌词海报生成 |
| **小红书** | 深度覆盖增强 | 在原版小红书解析基础上，支持配置无水印原图解析下载 |

> 其余 B 站、抖音、快手、微博、小黑盒、知乎、A 站、油管、X 等常见平台全部直接由原插件官方解析器负责。

---

## 💡 架构优势

1. **零冗余开销**：扩展包自身不启动重复的消息监听管线、不启动重复的下载器会话、不重复运行后台缓存清理定时任务，全部复用原插件核心引擎。
2. **完全同步原插件更新**：原插件后续若升级 HTML 卡片样式、优化合并转发逻辑、改进防抖与仲裁协议，扩展包追加的平台**100% 自动享受最新特性**。
3. **全局配置与开关联动**：在群聊中执行 `/关闭解析` 与 `/开启解析`，对原插件及本扩展包的所有解析器一视同仁生效。
4. **热重载自动感知**：在 WebUI 后台重载原插件时，扩展包后台守望者将在 3 秒内全自动静默重新注入，无需手动干预。

---

## ⚙️ 配置说明

在 AstrBot 插件管理面板中配置本插件：
- **扩展解析器列表**：通过原插件同款的 `template_list` 选择框，按需开启或关闭上述 5 个解析器，并为各解析器单独配置特定参数（如 Telegram API 凭据、网易云 NodeJS 地址、小红书原图开关等）。
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

- 基础插件与核心架构：[Zhalslar/astrbot_plugin_parser](https://github.com/Zhalslar/astrbot_plugin_parser)
- 解析原型：[fllesser/nonebot-plugin-parser](https://github.com/fllesser/nonebot-plugin-parser)
- 网易云接口增强：[NeteaseCloudMusicApiEnhanced](https://github.com/neteasecloudmusicapienhanced/api-enhanced)