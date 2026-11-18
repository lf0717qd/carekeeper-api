# CareKeeper API

乡音健康管家 V1.1 的后端：粤语对话 + GLM-ASR-2512 语音识别。

## 接口

- GET /api/health
- POST /api/chat
- POST /api/chat_stream
- POST /api/asr  { "audio_base64": "...", "mime": "audio/wav" }

## Render 部署

1. 在 Render 新建 Blueprint，选择本仓库；
2. Render 读取 render.yaml；
3. 在环境变量里填 API_KEY（只放服务端）；
4. 部署完成后，把前端 config.js 的 apiBase 改成 Render 地址。

## 注意

- 免费 Web Service 15 分钟无流量会休眠，冷启动约 1 分钟；
- 免费实例本地文件不持久，本项目不依赖本地文件；
- 不要把 API_KEY 写进代码或前端；
- Demo 不诊断、不开方，只做健康教育/就医引导。
