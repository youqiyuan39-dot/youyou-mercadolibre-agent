# 悠悠 Mercado Libre Agent

Windows 本地商品采集、SKU证据整理、AI辅助编辑、定价、审核与发布工作台。此仓库更新自2026-10-23验收源码，并补充2026-10-25跨电脑恢复支持。

**当前可恢复程序与私有业务文件；无人值守发布全链路仍未验收完成。** 缺失商品事实、同规格证据或有效费用时保持阻塞，不把测试通过写成业务成功。历史图片有效尺寸和逐SKU发布错误解析修复已包含在源码中。

## 新电脑快速开始

1. 安装 **Python 3.12 x64**、Chrome，克隆或下载完整项目。不要只下载EXE或单个页面。
2. 在项目目录运行：

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File tools/setup.ps1 -Assistant -Rag -Desktop
   ```

   安装会创建项目主环境 `.venv`、助手环境 `tools/assistant_env` 和检索环境 `tools/rag_env`，不会启动业务任务。可用 `-Python` 指定3.12路径；离线安装追加 `-Wheelhouse "D:\迁移包\wheelhouse"`。
3. 按 **[完整恢复指南](docs/RESTORE.md)** 导入私有业务包和模型运行包；没有旧数据也可空库运行。模型权重约6.6GB，不存放在Git中，指南提供固定版本来源、SHA256和下载脚本。
4. 双击 `启动新版工作台.cmd`，浏览器打开 `http://127.0.0.1:8790`。新版管理采集接收端8789。桌面窗口按指南构建并重建快捷方式，需要 WebView2。
5. Chrome 扩展管理加载 `collector_extension`，随后在工作台重新配置模型服务、店铺和OAuth授权。Windows DPAPI凭据不能跨电脑直接解密，不随仓库/迁移包发送。

## 程序与数据

| 内容 | 位置/恢复方式 |
| --- | --- |
| 后台、页面、扩展、桌面源码 | `src`、`web`、`collector_extension`、`selection_collector`、`desktop` |
| 知识与规则 | `knowledge/project`、公开配置与 `config/*.example.*` |
| 安装与验证 | `tools/setup.ps1`、依赖锁文件、`tests`（需Node.js 22+） |
| 私有商品、图片、审核、发布历史 | `tools/portable_data.py export/import`，私密传输 |
| 本地模型、推理引擎与检索权重 | `tools/restore_runtime.py` 导入，或按恢复指南重新取得 |
| 凭据、浏览器状态、账户配置 | 不导出；新设备重新授权 |

公开仓库不包含真实草稿、私有图片、账号记录、密钥、浏览器缓存、模型大文件或本机虚拟环境。迁移导入有SHA256校验、拒绝覆盖、路径重定位及自动任务暂停标记。重新授权前不能直接续跑旧发布计划。

## 本地验证

本次独立恢复的范围和结果见 [恢复验收记录](docs/RESTORE_VALIDATION_20261025.md)。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

5个依赖历史私有采集原件的回放用例在无私有材料时显式跳过。其余用例使用隔离数据及模拟平台；外部账号、模型硬件性能、真实供应商页面和发布必须在目标电脑分别验收。

正式发布已在新版界面提供，必须通过具体店铺/SKU/图片/费用/授权检查。发布未知结果先回查，禁止盲目重发；品牌规则为Generic，利润要求为贡献利润÷买家售价严格大于10%。预算缺项不按0处理，不用竞品尺寸自动替代本品事实。

许可证与第三方来源见 [LICENSE](LICENSE)、[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
