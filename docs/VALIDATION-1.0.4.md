# 1.0.4 BOSS 弹窗事件回调修复验证

日期：2026-09-14。环境：Windows x64、Python 3.11、Patchright 1.62.3、PyInstaller 6.22.2。

## 根因

用户日志中的错误为：`'builtin_function_or_method' object has no attribute '_pw_impl_instance_'`。

1.0.3 在 `BrowserAutomation.apply()` 注册弹窗事件时传入 `popups.append`。Patchright 的 `ImplToApiMapping.wrap_handler()` 会在回调对象上保存包装器属性；Python 内置列表方法不支持设置该属性，因此注册时即失败，尚未导航到职位页或点击“立即沟通”。

此前发送测试主要从 `_boss_message()` 开始；涉及 `apply()` 的关闭异常测试使用了模拟 `page.on()`，遗漏了真实驱动对回调的包装。

## 修复

- 用普通函数 `lambda popup: popups.append(popup)` 注册事件，兼容驱动的包装器缓存。
- 把注册操作移入 `apply()` 的异常处理和页面清理范围，注册失败时也能关闭本次新建页面。
- 核对应用内其他事件监听：其余回调均为普通函数、lambda 或实例方法，不存在相同的内置方法用法。
- 保留完整聊天页图文发送、城市映射和投递去重逻辑。未修改用户已有待核实记录。

## 回归验证

新增 `tests/test_boss_apply.py`，使用产品实际 `_driver()`、`_context()`、`_page()` 和 `apply()`。仅将浏览器启动调整为隔离无头环境，不模拟 `page.on()`、`_boss_message()` 或消息发送方法。

- 两条路径分别覆盖同页跳转和 `window.open` 新标签页。
- 每条路径验证“立即沟通”、自定义文字、图片各执行一次并收到模拟送达回执；结束后关闭任务页及弹窗，保留原搜索页。
- 所有固定测试页面均由内存响应，其余请求中止；不使用真实账号或消息。
- 内存恢复旧回调写法后：**2 failed in 2.45s**，两条路径均复现同一 `_pw_impl_instance_` 错误。未回退磁盘源码。
- 修复后的定向测试：**2 passed in 7.17s**。
- 完整命令 `.venv\Scripts\python.exe -X utf8 -m pytest -q`：**78 passed、32 subtests passed**，42.93 秒；另有依赖产生的 2 条弃用提示。

## EXE 验证

- 构建命令：`.\build.ps1`。
- 产物：`dist/Get-Jobs-Python-1.0.4.exe`，**94,573,601 字节**。
- SHA-256：`5734dc0aaffe6f739aa8d4cba70621cc993eaa36fe6193cf57da03a36517df71`。
- 从 EXE 的 `PYZ.pyz` 提取实际应用模块，以真实 Patchright 执行上述两条完整 `apply()` 回归，均通过；同时验证包内 Patchright 回调包装器接受普通函数。
- 独立端口 `8766`、独立临时数据目录启动 EXE，健康接口版本为 `1.0.4`；首页、JavaScript、CSS 与源码逐字节一致，界面版本正确。
- 包内原有 373 个城市和 87 个别名完整。隔离验证实例已关闭。
- `git diff --check` 通过。

这些检查证明此回调兼容错误已被复现并修复；没有通过真实账号发送消息或验收线上送达。使用新版前退出旧版后台，保留 EXE 同级 `data/`。
