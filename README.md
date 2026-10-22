# WebChapterVerge · 网页小说整理

<img src="assets/AppIcon.png" width="112" alt="网页小说整理图标">

由于我们日常阅读的大量精神食粮，会出现txt版本找不到、只有少数网页在线阅读版本的情况。我们的需求如果直接要求ai帮助我们导出成品txt，会由于版权的限制无法工作。于是特此搞出这个小工具，能够规避ai的限制，将网页版小说整理为一本 TXT：收集章节和分页链接，在后台保存网页，修复换行并按书名导出。全部在本地运行，无需 AI 或 API Key。

## 安装与文件用途

下载整个仓库、解压后，双击最外层的 **WebArchive-1.1.0-macOS-universal2.dmg**，将 App 拖入“应用程序”，然后双击启动。

| 文件或文件夹 | 用途 |
| --- | --- |
| [WebArchive-1.1.0-macOS-universal2.dmg](WebArchive-1.1.0-macOS-universal2.dmg?raw=true) | macOS 完整安装包，已内置 Python 和 Tk，不需要运行命令 |
| README.md | 当前使用说明 |
| src/ | 程序源代码 |
| assets/ | 图标与可编辑绘图源文件 |
| docs/ | 安装说明、第三方许可和验证记录 |
| scripts/、WebArchive.spec、requirements-build.txt | 打包与验证工具 |
| tests/ | 功能测试 |
| VERSION、SHA256SUMS.txt | 版本号与安装包校验值 |

网页收集与保存需要本机安装 **Google Chrome**。本地 TXT 整理不需要浏览器。安装包包含 Apple Silicon 和 Intel 代码，最低系统声明为 macOS 11；尚未做各旧版系统和 Intel 实机验证。

当前未完成 Apple Developer ID 签名与公证，互联网下载后的首次启动可能受到系统安全检查；参见 [Apple 官方说明](https://support.apple.com/zh-cn/102445)。

## 使用方法

1. 在“网页网址”中粘贴目录或章节入口，点击“收集章节链接”。已有完整章节清单时可直接粘贴或导入。
2. 确认清单后点击“后台保存网页”。浏览器在后台运行，每个新批次建立独立任务缓存；“继续 / 重试”只处理当前任务未保存成功的页面。
3. 确认自动识别的“小说名称”，选择“输出目录”，点击“导出 TXT”。输出目录只新增一本 **书名.txt**，不生成报告、目录清单或时间子文件夹。同名文件自动加编号，保留旧文件。
4. 成功导出后，可点击“清理本次网页”，选择将本次使用的网页移到废纸篓；也可以保留缓存。TXT、其他任务以及未参与本次导出的文件不受影响。
5. 已有本地网页时，点“选择已有网页”，选择同一本小说的文件夹，再确认名称和输出目录导出。

默认输出目录为 `~/Documents/小说TXT`，选择其他目录后会记住。左侧“最近的小说任务”可以切换已保存的小说。

## 后台运行与登录

日常收集、保存使用没有窗口的后台 Chrome，创建后台标签，不调用切到前台的操作。只有主动点击“网页登录”才打开专用 Chrome 窗口。登录后回到工具开始任务，会退出专用登录窗口并用相同配置转为后台模式，保留登录资料。不会操作日常使用的 Chrome 配置。

## 书名、整理规则与清理范围

- 优先从网页中的书名信息识别名称，再参考页面标题；名称可手动修改。无法可靠识别时要求填写书名，不使用“整合”作为文件名。
- 沿用目录或清单顺序，同人和番外保留原位置，区分第一部与第二部。
- 合并章内数字分页，修复网页硬折行与明确的「」断行，删除独立的“本章未完，点击数字分页继续阅读”提示。
- 正文非空白字符校验通过后才导出。原始标点疑点不会擅自改写。
- 导出目录只放 TXT；任务记录留在应用数据目录，临时整理报告完成后回收。
- 存在未保存页面、读取失败、未识别文件或已检测到的缺页时，界面提示结果不完整，清理按钮不可用。
- 清理前重新核对 TXT 和源网页的校验值；文件被删除、移动或改动时会停止清理。仅将本次已校验的网页移到废纸篓，不递归删除用户文件夹。

## 识别范围

默认支持 `/book/书号/章节.html`、`/view/数字.html`、`/chapter/`、`/read/` 等形式。章节入口从当前章沿明确的“下一章”链接继续，不自动补入口之前的章节；目录入口按目录原顺序，检查同系列目录分页和章内分页。

高级设置默认隐藏，一般不用修改。其他网址形式可以填写正则或 CSS 选择器；误将普通网址粘贴进正则栏会自动移回网址框。按钮目录、无限滚动、登录验证及特殊路由仍可能需要站点适配，不保证适用于任意网站。

## 本地数据

App 的任务缓存、清单、设置、日志和专用浏览器资料放在 `~/Library/Application Support/WebArchive`。新的网页缓存按小说任务分别保存，不再堆在 TXT 输出目录。旧版已经保存的网页不会自动移动或删除。

不要将日常使用的“批量网页保存工具”目录、浏览器资料或应用数据目录上传到 GitHub。本仓库仅用于交付代码与完整安装包。

## 开发与构建

源码需要 macOS 和带 Tk 的 Python 3.13。网页功能另需 Chrome。

```sh
python3 src/网页批量保存.py
PYTHONPATH=src python3 tests/整理功能自检.py
PYTHONPATH=src python3 tests/链接收集自检.py
PYTHONPATH=src python3 tests/链接界面自检.py
PYTHONPATH=src python3 tests/小说工作流自检.py
PYTHONPATH=src:tests python3 tests/桌面流程自检.py
PYTHONPATH=src python3 tests/功能自检.py
```

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.txt
.venv/bin/python scripts/build_macos.py
```

默认在 `release/` 生成 DMG 与校验值。universal2 构建需要 Python、Tk 和全部二进制依赖同时包含 arm64 与 x86_64。可使用 `scripts/verify_bundle.py` 验证打包版本。

命令行 `--organize` 默认也只导出书名 TXT；开发诊断时才使用 `--full-report`。图标绘制程序在 `assets/draw_icon.swift`。

## 上传到 GitHub

将本目录内容上传到仓库根目录，包含最外层的完整 DMG。别人通过“Code → Download ZIP”下载并解压后，直接打开最外层的 DMG 安装。更新版本时，替换安装包、校验文件和 README 中的文件名，并删除仓库里的旧版安装包。

尚未选定本项目的开源授权协议；运行库许可见 `docs/THIRD_PARTY_NOTICES.txt`。
