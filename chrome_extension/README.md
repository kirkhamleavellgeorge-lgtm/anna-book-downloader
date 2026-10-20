# Chrome 连接器

首次使用，在已登录网站的 Chrome 配置中打开 `chrome://extensions`，启用开发者模式，选择「加载已解压的扩展程序」，选中本目录。扩展会打开连接说明。

本机图书助手生成 `local-config.json`，包含仅用于本机连接的 WebSocket 地址与令牌。配置不需要手动填写，不包含网站密码或 Cookies。启动助手后，点击本扩展的「重新连接」。之后可关闭说明页面，后台连接会继续运行。

扩展只读取 Anna 网站中由助手创建的页面，并管理由助手明确发起的下载。Chrome 中其他页面和普通下载不受影响。网站要求手动验证码时，仍需在网页内完成。请保留 Chrome 自身的下载安全提示。

临时下载位于 Chrome 默认下载目录的 `AnnaBookHelper` 子目录；本机助手会校验文件并保存到用户指定目录。连接程序仅监听 `127.0.0.1`，扩展每次重新连接都会读取最新本机配置。

支持 Chrome 116 及更新版本。活动连接每 20 秒发送心跳；程序未运行时，Chrome 120 及更新版本约每 30 秒尝试重新连接，Chrome 116–119 约每 60 秒尝试。也可随时点击扩展按钮手动重连。

实现依据：[Chrome 官方 WebSocket 生命周期示例](https://developer.chrome.com/docs/extensions/how-to/web-platform/websockets)、[下载 API](https://developer.chrome.com/docs/extensions/reference/api/downloads)、[扩展生命周期](https://developer.chrome.com/docs/extensions/develop/concepts/service-workers/lifecycle)。
