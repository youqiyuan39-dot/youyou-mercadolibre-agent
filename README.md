# Youyou Mercado Libre Agent

本地运行的 Mercado Libre 商品采集、AI 辅助编辑、审核与发布工作台。支持把同一份源码放到不同的 **Windows 10/11** 电脑运行；Python、Chrome 和授权需要在每台电脑分别配置。项目不会自动把私人商品或密钥上传到 GitHub。

## 新电脑安装与启动

1. 安装 Python 3.11+ 和 Google Chrome。项目代码只用 Python 标准库。下载或克隆**完整项目文件夹**，文件夹可以放在任意用户目录。
2. 双击 `启动工作台.cmd`，在浏览器打开 `http://127.0.0.1:8789`。这是经典工作台，包含现有采集、编辑、审核和发布请求预览链路。
3. 若要使用新版总览、图片建品、店铺运营和模板界面，双击 `启动新版工作台.cmd`，打开 `http://127.0.0.1:8790`。新版可以独立接收采集，也能与经典版同时使用同一个项目目录。**新版正式发布入口目前保持关闭**；需要发布时回经典工作台完成预览、审核和确认。
4. Chrome 打开 `chrome://extensions`，开启开发者模式，加载项目内 `collector_extension` 文件夹。扩展公钥固定 ID，换电脑或移动文件夹后仍与本机服务匹配。采集浮窗和扩展弹窗自动连接 8790 或 8789，无需手填旧连接码。旧版同名扩展应停用，避免重复浮窗。
5. 在“AI 全局设置”输入自己的模型服务地址、模型名和 API Key；在“店铺管理”输入自己的 Mercado Libre 开发者应用配置并重新授权。密钥和 OAuth 令牌使用当前 Windows 用户的 DPAPI 加密，**不能从另一台电脑复制解密**。未配置密钥时仍可本地采集与编辑；真实 AI 调用和发布需要各自的有效配置。

`127.0.0.1` 总是指运行工作台的这台电脑。两个端口被其他服务占用时，先关闭旧进程；启动窗口应保持打开。不要同时在两台电脑上运行同一 OAuth 隧道。

## 私有数据迁移

公开仓库只含代码和必要的通用图标／嵌入页面资源，不含草稿、图片、发布记录、店铺数据或密钥。迁移到另一台 Windows 电脑时：

1. 先关闭旧电脑工作台。在旧电脑项目根目录执行：
   `python tools/portable_data.py export ..\youyou-data.zip`
2. 通过自己控制的私密方式把 ZIP 传到新电脑，**不要提交到 GitHub**。这个 ZIP 未加密，内含商品与业务数据。
3. 在新电脑的全新项目目录执行：
   `python tools/portable_data.py import ..\youyou-data.zip`
   导入前会检查路径和目标文件；已有同名文件时停止，不覆盖。包含草稿、原图、AI 输出、审核和发布记录、店铺缓存与非密钥模型配置。旧 `data/workflow_board.json` 是可重建缓存，不迁移。
4. 新电脑重新保存 API Key、OAuth Client Secret 并重新授权店铺。检查图片、草稿、发布记录和 SKU × 站点价格，再进行任何付费模型调用或真实发布。

`.secrets/`、令牌、隧道凭据和日志不会进入迁移包。原电脑文件不被导出工具修改。源文件若包含旧电脑的绝对路径，应通过编辑器重新选择本机文件；项目自身的源码和静态资源均通过项目相对路径加载。

## OAuth 回调与隧道

Mercado Libre Redirect URI 必须与开发者应用后台登记的 HTTPS 地址完全一致。若使用自己的 Cloudflare Tunnel，将 `config/cloudflared-mercadolibre.example.yml` 复制为 `config/cloudflared-mercadolibre.yml`，填写自己的 UUID 和 hostname；将该隧道凭据安装到当前 Windows 用户的 `%USERPROFILE%\.cloudflared\<UUID>.json`，安装 `cloudflared` 后执行 `tools/start-mercadolibre-tunnel.ps1`。脚本根据当前用户目录生成运行配置，不写死开发者电脑路径。新域名还需同步更新开发者后台及工作台 Redirect URI。工作台本身不需要隧道即可启动。

## 验证与开发

在项目根目录运行 `python -m unittest discover -s tests -v`。代码与页面分别在 `src/`、`web/`，采集扩展在 `collector_extension/`。正式发布、付费模型和真实 OAuth 回调需要自己的账号与凭据；自动测试使用本地模拟，不代表这些外部调用已在新电脑通过。

项目使用 MIT 许可证；第三方来源见 `THIRD_PARTY_NOTICES.md`。