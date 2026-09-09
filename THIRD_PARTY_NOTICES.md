# 来源与授权说明

本项目参考 [loks666/get_jobs](https://github.com/loks666/get_jobs) 的功能和浏览器操作流程，以 Python 重新实现后端，并编写新的本地管理界面。

- 原作者：GET JOBS / loks666。
- 参考分支：main。
- 本次参考提交：`f8094286b4768c2b0e3b1f6fc98011482c7ac16a`。
- `jobs_app/browser.py` 中的平台地址、城市编码、页面定位规则和部分操作流程参考原项目的 `src/main/java/com/getjobs/worker/`。
- 原项目的 GET JOBS Non-Commercial License 1.0 原文保留于根目录 [LICENSE](LICENSE)。本实现保留该非商业使用限制及原作者署名；商业用途需要原权利人授权。
- `.reference/get_jobs/` 是本机用于核对功能的原始仓库，已从当前项目的版本控制和构建范围排除。

Python 运行库依赖各自的许可证，PyInstaller 会按其默认机制收集相关依赖。项目未复制原项目前端品牌图片、用户简历或账户数据。

从 1.0.1 起，BOSS 使用 [Patchright Python](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python) 1.62.3 驱动（Apache-2.0，Vinyzu / Kaliiiiiiiiii），其他平台继续使用 Playwright。构建收集两个依赖包及其分发元数据。程序未复制上游的页面计时器、console 或 Function.prototype 覆盖脚本。
