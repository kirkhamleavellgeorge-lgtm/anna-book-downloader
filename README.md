# Anna 图书下载助手

Windows 桌面程序：连接你正在使用的 Chrome，复用该浏览器配置的网站登录状态。一次粘贴或导入书单，自动在 Anna's Archive 搜索、匹配版本，进入普通免费排队页并保存文件。仅用于你有权获取的作品。无需会员、API 密钥。

## 使用

1. 双击 **启动程序.bat**。首次启动自动创建 `.venv` 并安装依赖，需要网络和 Python 3.10+。
2. 首次点击 **连接已登录 Chrome**，按窗口说明加载 `chrome_extension` 文件夹中的扩展。连接成功后会显示 **已连接 Chrome**。
3. 输入书单，每行一本；也可导入 UTF-8 或 GB18030 编码的 TXT。
4. 选择保存目录、语言和优先格式，点击 **开始下载**。
5. 如果搜索到多个分集 / 分册，会弹出勾选窗口。选择需要的册数后点击 **下载所选分册**；可全选、全不选、跳过这本书，或停止整个队列。同一册的不同文件格式归为一项，程序按优先格式依次尝试。
6. 浏览器自动检查会自行等待，无需操作。只有网站明确要求验证码时才会提示手动完成；通过后自动继续。请保留 Chrome 窗口，直到队列结束。

输入示例：

```text
Pride and Prejudice | Jane Austen
红楼梦 | 曹雪芹
```

也支持 ISBN。`| 作者` 可省略；书名相近或同名书较多时建议填写作者或 ISBN。语言是筛选条件，优先格式是排序偏好，找不到时可能下载其他格式。每册自动尝试最多三个匹配版本，每个版本最多三个普通免费通道；不会通过切换地址绕过网站限额。

中文书名支持“系列名 + 空格或句点 + 分册名”的记录，例如输入“科学帮帮忙”可匹配“科学帮帮忙  万能的磁铁”。同一行搜索到多个分册、分集或不同书名时，会列出供你勾选；未勾选的不会下载。确认后每册单独显示下载结果，某册失败时只尝试该册的其他版本，不替换成其他集数。关闭勾选窗口会跳过当前书名，继续下一行。仅匹配到一册时自动下载。

默认保存在项目的 `downloads` 目录。双击已完成的表格行可打开图书；CSV 可以导出本次结果。输入书单和选项会保存在本机，下次打开无需重输。成功记录保存在 `data/history.json`；重新开始会再次搜索，允许勾选其他分册，并验证文件 MD5、跳过已经完成的分册。停止时只关闭本程序创建的标签页，保留原有 Chrome 页面。重新开始会重试未完成的书籍，未完成的大文件会重新传输。

## 浏览器及验证

使用当前 Chrome 登录状态需要首次加载本地扩展，这是 Chrome 的安装步骤：

1. 在已登录 Anna 网站的 Chrome 配置中打开 `chrome://extensions/`。
2. 开启右上角“开发者模式”，点击“加载已解压的扩展程序”。
3. 选择本项目的 `chrome_extension` 文件夹。程序的连接窗口可以复制路径、打开该文件夹。
4. 等待程序显示“已连接 Chrome”。之后运行程序时扩展会自动尝试重新连接，也可点击扩展图标中的“重新连接”。

扩展只在安装它的 Chrome 配置中生效。Chrome 账户已登录，并不等于已登录 Anna 网站；网站登录及验证仍由当前 Chrome 管理。程序不导出或复制 Cookies，不访问个人 Chrome 数据目录，不重启你的浏览器。扩展未连接时会要求先连接，避免另开一个未登录的会话。

扩展使用 Chrome 下载接口将本程序的文件暂存到 Chrome 下载目录下的 `AnnaBookHelper`，Python 再复制、校验并保存到所选目录，随后清理这笔下载的暂存文件。只管理扩展创建的下载 ID；不接管用户在其他页面启动的下载。若个别免费通道直接触发浏览器下载而没有公开的下载链接，需要在 Chrome 中手动处理。

连接服务只监听本机 `127.0.0.1`，每次启动生成新的连接令牌。`chrome_extension/local-config.json` 自动生成，不要分享此文件或同时运行多个本项目的实例。

免费通道存在排队、验证码、限速、限额和不可用情况，不能保证只输入书名后始终无人值守。程序等待网站自己的倒计时和公开下载链接，不自动处理验证码、不调用会员接口。遇到 HTTP 429 或已识别的下载限额提示时停止整个队列。

验证状态会区分“浏览器自动检查中”和“等待手动验证码”。普通 JavaScript 浏览器检查由网站在正常浏览器中运行，程序自动等待结果并续跑；超过 30 秒仍未通过时会提示“等待网站放行”。明确要求点击、填写的验证码仍需手动完成。是否再次验证由网站决定，不保证自动通过，也不会刷新页面反复尝试绕过检查。

当前默认验证等待最多 15 分钟、免费排队最多 30 分钟、文件传输最多 60 分钟。可在 `anna_downloader/browser.py` 的 `Options` 中调整。

程序会校验文件类型和网站记录的 MD5，不会把 HTML 验证页当成书籍保存。MD5 用于完整性比对，不代表来源可信或内容安全。

## 故障排查

- 显示“Chrome 扩展未连接”：确认本程序已打开，扩展位于你使用的那个 Chrome 配置中；点击扩展图标重新连接。移动项目目录后需重新加载该目录。
- 一直显示“浏览器自动检查中”或“等待网站放行”：先看浏览器能否正常访问网站；只有网站放行后程序才能继续。若提示“等待手动验证码”，请手动完成。程序不能解决网络封锁或网站持续拒绝访问；可停止后在界面选择另一个官方域名。
- 没有匹配版本：检查书名、作者和语言；尝试 ISBN。“不限语言”会放宽语言条件。匹配并不保证出版版次、译者一致。
- 下载页结构变化：日志位于 `data/application.log`，最近一次报告位于 `data/last_run.json`。在命令行运行 `.\.venv\Scripts\python.exe -m anna_downloader` 可直接查看启动错误。
- 缺少 Python：安装包含 Tkinter 和 pip 的 Python 3.10+ Windows 版本后重新运行启动脚本。

## 验证

```powershell
python -m unittest discover -s tests -v
```

自动化测试使用本地 HTTP 测试站及生成的测试图书，覆盖完整搜索、匹配、排队、下载、校验和重复跳过流程，不消耗网站下载额度。原有浏览器测试使用 Edge；Chrome 扩展集成测试使用独立的 Playwright Chromium 测试配置，验证 HttpOnly 登录会话复用、下载文件完整性，以及用户原有标签页保留，不操作你的日常浏览器配置。没有安装测试用 Chromium 时该集成测试会跳过，可运行 `.\.venv\Scripts\python.exe -m playwright install chromium` 安装。实际 Anna 网站是否允许当前登录会话通过验证，仍取决于网站。

实现依据：网站公开的[免费通道说明](https://annas-archive.pk/faq#slow)、[搜索结果模板](https://software.annas-archive.gl/AnnaArchivist/annas-archive/-/blob/main/allthethings/templates/macros/aarecord_list.html)、[排队页模板](https://software.annas-archive.gl/AnnaArchivist/annas-archive/-/blob/main/allthethings/page/templates/page/partner_download.html)，以及 [Chrome 本地扩展加载说明](https://developer.chrome.com/docs/extensions/get-started/tutorial/hello-world#load-unpacked)、[下载接口](https://developer.chrome.com/docs/extensions/reference/api/downloads)、[浏览器调试连接限制](https://developer.chrome.com/blog/remote-debugging-port)。
