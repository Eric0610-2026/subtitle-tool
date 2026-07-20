# 字幕生成与双语翻译工具

基于 faster-whisper 的本地字幕生成 + AI 翻译工具。Windows 桌面 GUI，支持视频/音频文件拖拽处理，一键生成双语字幕。

## 功能

- **语音转写**：本地 faster-whisper-large-v3-turbo 模型，支持 CUDA 加速
- **AI 翻译**：OpenAI 兼容接口（DeepSeek / 任意 API），批量翻译 + 缓存去重
- **双语字幕**：原文 + 译文上下对照，支持繁简转换
- **MKV 内嵌**：自动将字幕软内嵌到 MKV 文件
- **断点续转/续翻**：崩溃或中断后可从中断处继续
- **并行流水线**：转写与翻译并行执行，提升效率
- **进度跟踪**：实时进度条 + ETA 估算
- **拖拽操作**：支持文件/文件夹拖拽到列表

## 系统要求

- Windows
- Python 3.10+
- NVIDIA GPU（推荐，支持 CUDA 加速；也可用 CPU）

## 所需文件

### ffmpeg.exe / ffprobe.exe（必需）

从 [ffmpeg.org](https://ffmpeg.org/download.html) 下载 Windows 版本，将 `ffmpeg.exe` 和 `ffprobe.exe` 放入项目根目录。**两个文件缺一不可**，ffprobe 用于获取视频时长和验证输出文件。

### 语音识别模型

从以下地址下载 faster-whisper-large-v3-turbo 模型（约 1.6 GB），放入项目根目录的 `faster-whisper-large-v3-turbo/` 文件夹：

https://www.modelscope.cn/models/pengzhendong/faster-whisper-large-v3-turbo/summary

## 快速开始

```bash
# 1. 克隆仓库
git clone https://github.com/Eirc0610/subtitle-tool.git
cd 仓库名

# 2. 安装依赖
pip install -r requirements.txt

# 3. 放置 ffmpeg.exe 和 ffprobe.exe（必需）到项目根目录

# 4. 下载模型到 faster-whisper-large-v3-turbo/ 目录

# 5. 配置 API
copy zimu_app\config.example.json zimu_app\config.json
# 编辑 config.json，填入 translation.api_key 和 translation.api_url

# 6. 启动
python subtitle_app.py
```

或双击 `启动字幕工具.bat`（以 pythonw.exe 无控制台启动）。

### 启动检查

应用启动后会自动检测以下项目，缺少的项目会以黄色警告显示在日志区：

- ✅ ffmpeg.exe 是否存在
- ✅ 语音识别模型是否已下载
- ✅ API 地址和密钥是否已配置

如未配置翻译 API，程序仍可正常使用转写功能，仅翻译功能不可用。

## 配置文件

所有参数集中在 `zimu_app/config.json`（需自行从 `config.example.json` 复制创建）：

| 字段 | 说明 |
|---|---|
| `translation.api_key` | API 密钥 |
| `translation.api_url` | API 地址 |
| `whisper.device` | 计算设备（cuda / cpu） |
| `whisper.model_dir` | 模型路径 |

修改配置后需重启应用生效。

## 项目结构

```
├── zimu_app/
│   ├── qt_app.py          # Qt 主窗口 UI
│   ├── transcriber.py     # 音频提取 + Whisper 转写
│   ├── translation.py     # AI 翻译客户端
│   ├── translator.py      # 翻译阶段编排
│   ├── pipeline.py        # 流水线编排
│   ├── srt_utils.py       # SRT 解析/写入/进度
│   ├── muxer.py           # MKV 软内嵌
│   ├── dialogs.py         # 设置/历史对话框
│   ├── config.py          # 配置加载
│   └── config.example.json # 配置模板
├── tests/                 # 单元测试
├── subtitle_app.py        # 入口
└── 启动字幕工具.bat       # 快速启动
```

## 测试

```bash
python -m unittest discover -s tests
```

## 注意事项

- 首次运行会自动下载模型（约 1.6 GB），或手动放置到 `faster-whisper-large-v3-turbo/`
- 翻译缓存文件 `.subtitle_translation_cache.json` 超过 10000 条时会自动裁剪

## 技术栈

- PySide6（Qt 桌面 GUI）
- faster-whisper（本地语音识别）
- ffmpeg（音频提取 / 字幕内嵌）
- OpenAI Chat Completions API（翻译接口）
