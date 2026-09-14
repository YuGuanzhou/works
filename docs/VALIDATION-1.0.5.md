# 1.0.5 BOSS 薪资显示修复

日期：2026-09-14。环境：Windows x64、Python 3.11、PyInstaller 6.22.2。

## 原因与依据

BOSS 页面通过专用字体显示薪资数字，DOM 文本使用 Unicode 私用字符。原采集器直接保存该文本，本地页面没有对应字体，显示为方框，月薪筛选也无法解析范围。

- 当前编码 `U+E031..U+E03A` 对应 `0..9`。依据 [BOSS 官方 CSS](https://static.zhipin.com/zhipin-geek-spa/web/v6737/static/css/app~0.7c23baca.css) 中的字体声明，将[混淆字体](https://img.bosszhipin.com/static/file/2023/30k9dfumyv1693967587404.ttf)与[普通字体](https://img.bosszhipin.com/static/file/2022/16a48d9v961651026858984.ttf)逐字比对全部轮廓端点、坐标及曲线标志，10 位数字完全一致。
- 同时兼容本地参考实现 `Boss.java` 的 `U+E8F0..U+E8F9` 数字映射。
- 当前混淆字体 SHA-256：`88f4b9338bc6939285b2f95f1a217b6cb7862f898a75dd70a285f1fa24386d6d`。
- 当前普通字体 SHA-256：`adc008fb1f72fbc18ca79bbcf74d6a6810bd26b697582d2ad77364103d01fea0`。

## 修改

1. 公共薪资规范化函数还原两套数字编码，保留薪资单位、多薪后缀及其他字符。
2. BOSS 采集和保存时规范化薪资；月薪解析复用转换，以恢复范围筛选。
3. 启动时仅更新已有 BOSS 记录中需要转换的薪资字段，修复可重复执行；列表、详情与 CSV 沿用存储结果。
4. 保留 1.0.4 的弹窗回调修复和已有完整聊天页发送流程。

## 验证

- `.venv\Scripts\python.exe -X utf8 -m pytest -q`：**83 passed、32 subtests passed**，44.10 秒；另有两条现存依赖弃用提示。
- 新增 `tests/test_salary_encoding.py` 的 5 项回归，覆盖两套完整映射、单位和多薪、采集/保存、历史迁移幂等、正常及其他平台记录保持原值、所有其他字段与投递流水保持不变、列表详情数据及 CSV。
- 已先通过 SQLite 备份 API 备份当前数据库，再仅修复 10 条历史记录的薪资。逐条结果与同记录已有明文薪资一致；修复前后所有其他记录字段和投递流水逐项相同，`PRAGMA integrity_check` 为 `ok`。备份保存在 EXE 同级 `data/backups/`，未纳入仓库。
- 本地实际运行页面的 10 条薪资均为正常数字，详情与列表一致；通过独立 Chromium 检查并截图至本地忽略目录 `build/salary-fix/records-repaired.png`。

## EXE 验证

- 构建命令：`.\build.ps1`；产物 `dist/Get-Jobs-Python-1.0.5.exe`，**94,574,945 字节**。
- SHA-256：`4d6daceccedf273bcd70ea2654564b74873dffaac34ff34e0972dfee71d1901b`。
- 从 EXE 内 `PYZ.pyz` 读取实际薪资模块，两套编码的转换、月薪解析与最低薪资筛选通过。
- 使用独立临时数据目录和空闲端口启动 EXE，预置两套编码的虚构薪资。启动后 API 与 CSV 分别返回 `10-15K`、`6-11K·13薪`；健康接口版本为 `1.0.5`，首页、JavaScript、CSS 与源码逐字节一致。
- 检查脚本：本地忽略目录 `build/salary-fix/verify_exe.py`。隔离验证实例及其临时数据已清理；用户现有服务未由此检查停止。
- `git diff --check` 通过。

以上检查未执行真实招聘投递，不重新计算已有评分或投递状态。使用后续采集修复须关闭旧版后台，再启动 1.0.5，并保留原 `data/` 目录。
