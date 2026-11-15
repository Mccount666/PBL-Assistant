# PBL 语音讨论助手

面向课堂小组讨论的 PBL（Problem-Based Learning）辅助系统：老师逐幕发放案例，学生围坐讨论，AI 导师按 PBL 规则适时介入，系统同步维护假设板、学习议题和知识点画布，课后导出讨论纪要。

## 功能

- **多幕案例**：按「第一幕 / 第二幕 / Act 1」逐幕发放，内置阑尾炎、肺炎案例
- **AI 导师**：只追问依据、激发怀疑、提示讨论维度，不给答案；API 模式与离线规则引擎双轨
- **实时语音**：浏览器麦克风 → 服务端代理 → 阶跃星辰 Realtime，支持语音转写与语音播报
- **假设板 / 学习议题**：记录疾病假设、支持/反对依据、待查问题
- **知识点画布**：从讨论和案例中提取症状/体征，关联疾病方向、分型与追问要点
- **文献检索**：Semantic Scholar / Europe PMC / Crossref 多源降级
- **讨论纪要**：一键导出 DOCX（假设演变时间线、议题清单、讨论摘录、复盘要点）
- **案例导入**：上传 Word / PPT / 图片，按幕次标题机械切分，预览确认后入库

## 技术栈

- 后端：FastAPI + SQLite（WAL）+ WebSocket
- 前端：原生 HTML / CSS / JavaScript
- 模型：任意 OpenAI 兼容接口（默认阶跃星辰），实时语音走 StepFun Realtime
- 文档解析：python-docx、python-pptx、Pillow；旧版 `.doc/.ppt` 由 LibreOffice 转换

## 快速开始

```bash
python3 -m venv venv
venv/bin/pip install -r requirements-server.txt

# 配置模型（也可以启动后在网页「⚙ 设置」里填写）
cat > .bedside_config.json <<'JSON'
{
  "mode": "api",
  "api_base": "https://api.stepfun.com/v1",
  "api_key": "YOUR_API_KEY",
  "model": "step-3.5-flash",
  "utility_model": "step-1o-turbo-vision"
}
JSON
chmod 600 .bedside_config.json

venv/bin/uvicorn voice_server:app --host 127.0.0.1 --port 8600
```

打开 `http://localhost:8600`。不配置 Key 时自动使用离线规则引擎。

也可以直接用环境变量：

```bash
BEDSIDE_MODE=api \
BEDSIDE_API_BASE=https://api.stepfun.com/v1 \
BEDSIDE_API_KEY=... \
BEDSIDE_MODEL=step-3.5-flash \
venv/bin/uvicorn voice_server:app --host 127.0.0.1 --port 8600
```

## 案例导入

在案例选择页点「＋ 导入案例」，支持：

- `.docx` / `.doc` / `.pptx` / `.ppt` / 常见图片
- 幕次标题：`第一幕`、`第1幕`、`幕一`、`【第二幕】`、`Act 1`、`Scene 2`
- 栏目：`学习目标`、`讨论问题`、`助教参考`（含 `期望假设` / `期望议题`）、`最终诊断`、`关键学习点`
- Word 内嵌图片按位置归幕，PPT 图片按页归幕；单独图片生成「图像资料」幕

详细写法见 [`docs/案例导入格式.md`](docs/案例导入格式.md)。

## 部署

`deploy_server.sh` 会创建 venv、安装依赖、注册 systemd 服务并做健康检查。生产环境建议：

- 用 Nginx 反向代理，保留 WebSocket Upgrade 头与长连接超时
- 配置 HTTPS（麦克风 `getUserMedia` 需要安全上下文）
- uvicorn 只监听 `127.0.0.1`
- 公网部署时给导入接口和会话接口加访问控制

## 目录

```
voice_server.py        FastAPI 主服务（API / WebSocket / 导入 / 导出）
core/                  导师引擎、假设板、文献、LLM 客户端、文档导入、纪要生成
cases/                 内置案例与知识库
voice_ui/              前端页面
docs/                  案例导入格式说明
data/                  运行时数据（SQLite、导入媒体，已 gitignore）
reports/               导出的纪要（已 gitignore）
```

## 注意

- `.bedside_config.json`、`data/media/`、`data/imports/`、`reports/*.docx` 已加入 `.gitignore`
- 导入接口当前默认无鉴权，公网部署请自行加口令或登录
- 本项目未附带 License，使用前请与作者确认