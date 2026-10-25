# 新电脑完整恢复

适用：Windows 10/11 x64、Python 3.12 x64、Chrome；运行测试还需 Node.js 22+ 在 PATH。项目目录可含中文与空格。不复制旧虚拟环境，必须在新路径重建。GPU 型号不同默认用 CPU；GPU 加速需要在本机核验后配置 device/gpu_layers。

## 取得材料

- GitHub：完整源码、网页、扩展、公用图标、项目知识库、测试、锁定依赖和配置示例。
- 本地私有迁移包：private-data.zip（草稿、图片、原始采集证据、审核、发布记录等），runtime-assets.zip（模型权重、llama.cpp DLL/EXE、检索模型），wheelhouse（Python 3.12/Windows x64 离线依赖）。这些文件不上传公开仓库。
- .secrets、浏览器缓存、Cookie、账号授权、日志、进程信息、订单/运营快照不迁移。店铺和模型密钥需重新配置。历史诊断截图/源码备份不是程序运行依赖，留在原机器。

## 恢复步骤

1. 安装 Python 3.12 x64、Chrome；桌面窗口还需要 Microsoft Edge WebView2 Runtime。克隆或解压完整代码到新目录。
2. 在项目目录 PowerShell 执行（仅本次脚本进程使用 Bypass，不修改机器全局执行策略）：

   `powershell -NoProfile -ExecutionPolicy Bypass -File tools/setup.ps1 -Assistant -Rag -Desktop`

   有离线依赖时追加 `-Wheelhouse "D:\迁移包\wheelhouse"`。若 python 命令不是3.12，追加 `-Python "C:\Python312\python.exe"`。依赖分为主程序、对话助手、检索三个独立环境。
3. 恢复模型运行文件：

   `.\.venv\Scripts\python.exe tools/restore_runtime.py "D:\迁移包\runtime-assets.zip"`

   逐文件验证 SHA256 后才写入；已存在同名文件会停止，不覆盖。若没有该包，见下方重新取得模型的方法。
4. 恢复业务数据：

   `.\.venv\Scripts\python.exe tools/portable_data.py import "D:\迁移包\private-data.zip"`

   完整校验后导入，将旧项目内部的 JSON 绝对路径重定位到新目录。禁止导入到已有业务数据的目录。导入会写入 `data/MIGRATION_REVIEW_REQUIRED.json`，暂停启动时的自动处理和自主任务；不清除发布未知状态或历史记录，不自动续发。冻结计划、旧费用和旧授权不能直接当作新机器已验收依据。
5. 双击 `启动新版工作台.cmd`，打开 http://127.0.0.1:8790。经典入口为 `启动工作台.cmd`；优先启动新版，由新版管理采集端8789，不同时手动启动两个抢占8789的服务。
6. Chrome 打开 chrome://extensions，开启开发者模式，加载 `collector_extension`。公钥固定扩展 ID，接收器来源校验与其一致。旧同名扩展停用，实际采集必须验证收到正确SKU。
7. 在店铺管理重新添加自己的店铺并完成 OAuth。旧记录仍保留旧店铺 ID，不能手工改 ID 来冒充重新授权；先核对账户归属，再处理历史发布回查。模型接口和付费图片服务需要各自配置。本地模型默认不转接付费接口。
8. 核对草稿数量、图片显示、SKU、历史发布 ID、同规格依据与当前费用；完成后备份并移走迁移暂停标记，再重启。自主发布还需在 `config/autonomous_flow.json` 明确配置目标店铺和授权，示例默认关闭。

## 桌面窗口

先执行 `powershell -NoProfile -ExecutionPolicy Bypass -File tools/build-desktop.ps1`，再执行 `powershell -NoProfile -ExecutionPolicy Bypass -File desktop/install.ps1 -DesktopShortcut`。快捷方式按当前目录生成，不覆盖已有入口。普通入口可以启动本项目后台；`--resident` 仅附着现有后台，不能用来首次启动。

## 无私有运行包时重新取得模型

`powershell -NoProfile -ExecutionPolicy Bypass -File tools/fetch-models.ps1 -Rag` 下载固定版本 Qwen3.5-9B GGUF及视觉组件并核对SHA256。来源为 https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/tree/3885219b6810b007914f3a7950a8d1b469d598a5 。模型源项目为 https://huggingface.co/Qwen/Qwen3.5-9B 。

llama.cpp 已用版本 b11552（记录的构建 23b0202a1），从 https://github.com/ggml-org/llama.cpp/releases/tag/b11552 取得 Windows CPU x64 运行包，解压全部依赖到 `runtime/llama`，不能只复制9KB启动EXE。CPU包历史SHA256为 `c00741179811ff903b515eae28bdeef1f47af6e81c49920c4e68b62954e056d6`。保留许可文件。复制 `config/local_model_runtime.example.json` 为 `config/local_model_runtime.json` 后按已安装文件修改；所有模型路径相对项目目录。模型约6.6GB，另需运行内存及检索缓存；在新硬件上另验速度和可用上下文。

## 验证范围

运行 `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`。5个历史真实私有采集回放用例在没有原始私有材料时显式跳过，其余合成/接口回归独立执行。测试不代表新电脑真实登录、GPU速度、供应商访问、付费模型或发布链路已通过。

原业务系统截至2026-10-23仍未完成无人值守发布闭环：同规格依据、费用与商品事实门禁继续生效。可恢复程序和业务文件不等于可以自动补造这些缺失事实。
