# Youyou Mercado Libre Agent

本地运行的 Mercado Libre 商品采集、AI 辅助编辑、审核与发布工作台。Chrome 扩展“悠悠采集”从 1688 商品页采集资料；发布前需人工核对。仅支持 Windows，目前使用 Windows DPAPI 保存 OAuth 和模型密钥。

## 在另一台 Windows 电脑启动

1. 安装 Python 3.11 或更新版本、Google Chrome。项目目前只使用 Python 标准库，无需 `pip install`。
2. 下载项目完整文件夹，双击 `启动工作台.cmd`，然后打开 `http://127.0.0.1:8789`。启动窗口须保持打开。端口 8789 如果被占用，先关闭旧实例。
3. 在 Chrome 打开 `chrome://extensions`，启用开发者模式，加载本项目 `collector_extension` 文件夹。扩展的公钥固定了 ID，移动项目文件夹或换电脑后服务端仍能识别。原电脑如果仍装有旧版“悠悠采集”，请停用旧版并加载此版本。
4. 在工作台“AI 全局设置”填写你自己的模型地址、模型名和 API Key；在“店铺管理”填写你自己的 Mercado Libre 开发者应用配置并重新授权。密钥只写入当前 Windows 用户的 DPAPI 加密文件，不能从其他电脑复制使用。
5. 商品采集可在未配置 OAuth 时使用。真实发布需要有效店铺授权、完整 SKU × 站点价格、人工审核和发布确认。

`127.0.0.1` 代表运行工作台的当前电脑，不是开发者电脑的地址。复制项目时请复制整个目录，不要只复制 `src` 或扩展文件夹。

## OAuth 回调和隧道

Mercado Libre 的 Redirect URI 必须与开发者应用后台登记的 HTTPS 地址完全一致。可使用自己控制的 Cloudflare Tunnel：复制 `config/cloudflared-mercadolibre.example.yml` 为 `config/cloudflared-mercadolibre.yml`，填写自己的 tunnel UUID 和 hostname；把该隧道的凭据安装到当前 Windows 用户的 `%USERPROFILE%\.cloudflared\<UUID>.json`，安装 `cloudflared` 后运行 `tools/start-mercadolibre-tunnel.ps1`。脚本按当前用户路径生成本机配置，不依赖开发者的账户目录。隧道凭据、域名控制权和 OAuth Client Secret 均不在此仓库中。

若沿用既有隧道，需由隧道所有者在新电脑自行配置凭据；若使用新域名，还要更新 Mercado Libre 开发者后台和工作台的 Redirect URI。启动工作台本身不需要隧道。
同一隧道迁移时，请先停止旧电脑上的隧道连接，避免 OAuth 回调落到错误的电脑。

## 私有数据迁移

公开仓库不含商品草稿、图片、发布记录、店铺资料和密钥。要在自己的两台电脑之间迁移，请把私有备份中的 `assets/`、`drafts/`、`draft_versions/`、`data/`、`jobs/`、`reviews/`、`ai_outputs/` 复制到新电脑项目根目录。不要把 `.secrets/`、日志、OAuth 隧道凭据放进 Git；新电脑需重新配置模型密钥和店铺授权。`data/workflow_board.json` 是缓存，移动后可删除并由工作台重建。

## 开发

在项目根目录运行 `python -m unittest discover -s tests -v`。代码与网页在 `src/`、`web/`，Chrome 扩展在 `collector_extension/`。本仓库按 MIT 许可证开放；第三方来源见 `THIRD_PARTY_NOTICES.md`。
