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

- Windows 10/11
- Python 3.10+（[下载地址](https://www.python.org/downloads/windows/)，安装时务必勾选 **Add Python to PATH**）
- NVIDIA GPU 推荐（支持 CUDA 加速；也可用纯 CPU，但转写速度慢约 10 倍）
- [Visual C++ Redistributable](https://aka.ms/vs/17/release/vc_redist.x64.exe)（PySide6 必需，缺少则启动闪退）

## 首次部署前必读

### ① 语音识别模型（必需，约 1.6 GB）

从以下地址下载整个 `faster-whisper-large-v3-turbo` 文件夹（包含 `model.bin`、`tokenizer.json`、`vocabulary.json`、`config.json` 等文件），放入 `models/` 目录（即 `models/faster-whisper-large-v3-turbo/`）：

https://www.modelscope.cn/models/pengzhendong/faster-whisper-large-v3-turbo/summary

### ② ffmpeg.exe / ffprobe.exe（必需）

从 [ffmpeg.org](https://ffmpeg.org/download.html) 下载 Windows 版本（选择 **Windows → Windows Builds → ffmpeg-release-full** 或 **gyan.dev** 的完整构建），将解压后 `bin/` 目录下的 `ffmpeg.exe` 和 `ffprobe.exe` 放入项目根目录下的 `tools/` 目录。**两个文件缺一不可**。

### ③ GPU 加速（可选，强烈推荐）

默认 `pip install -r tools/requirements.txt` 安装的是 CPU 版 torch，转写非常慢。如需 GPU 加速，请先运行以下命令安装 CUDA 版 torch，再安装其他依赖：

```bash
pip install torch --index-url https://download.pytorch.org/whl/cu121
pip install -r tools/requirements.txt
```

前提条件：NVIDIA 显卡 + [CUDA 12.x](https://developer.nvidia.com/cuda-downloads) + [cuDNN](https://developer.nvidia.com/cudnn)。

## 快速开始

```bash
# 1. 克隆仓库
git clone https://github.com/Eric0610-2026/subtitle-tool.git
cd subtitle-tool

# 2. 安装依赖
pip install -r tools/requirements.txt

# 3. 放置 ffmpeg.exe 和 ffprobe.exe（必需）到 tools/ 目录

# 4. 下载模型到 models/faster-whisper-large-v3-turbo/ 目录

# 5. 配置 API
copy subtitle_app\config.example.json subtitle_app\config.json
# 编辑 config.json，填入 translation.api_key 和 translation.api_url

# 6. 启动
python subtitle_app/subtitle_app.py
```

或双击 `启动字幕工具.bat`（自动安装依赖 + 以 pythonw.exe 无控制台启动）。

> **⚠ 首次启动较慢**：PySide6 和 torch 加载需要 20-30 秒，期间界面不会立即弹出，请耐心等待，不要反复点击。

### 启动检查

应用启动后会自动检测以下项目，缺少的项目会以黄色警告显示在日志区，ffmpeg/ffprobe 缺失还会弹出对话框：

- ✅ ffmpeg.exe 是否存在（项目根目录或 tools/ 目录）
- ✅ 语音识别模型是否已下载
- ✅ API 地址和密钥是否已配置

如未配置翻译 API，程序仍可正常使用转写功能，仅翻译功能不可用。

## 配置文件

 所有参数集中在 `subtitle_app/config.json`（需自行从 `config.example.json` 复制创建）：
 
 | 字段 | 说明 |
 |---|---|
 | `translation.api_key` | API 密钥 |
 | `translation.api_url` | API 地址 |
 | `translation.model` | 模型名称（如 `deepseek-chat`） |
 | `whisper.device` | 计算设备（cuda / cpu） |
 | `whisper.model_dir` | 模型路径 |
 | `app.default_video_dir` | 默认视频目录，记得改为你自己的路径 |
 
 修改配置后需重启应用生效。

## 项目结构

```
├── subtitle_app/
│   ├── qt_app.py          # Qt 主窗口 UI
│   ├── panels.py          # 进度/预览/日志面板
│   ├── widgets.py         # 拖放列表、日志条目控件
│   ├── transcriber.py     # 音频提取 + Whisper 转写
│   ├── translation.py     # AI 翻译客户端
│   ├── translator.py      # 翻译阶段编排
│   ├── pipeline.py        # 流水线编排
│   ├── srt_utils.py       # SRT 解析/写入/进度
│   ├── muxer.py           # MKV 软内嵌
│   ├── dialogs.py         # 设置/历史对话框
│   ├── config.py          # 配置加载
│   ├── config.example.json # 配置模板
│   └── subtitle_app.py     # 入口
├── cache/                 # 运行时缓存文件
├── models/                # Whisper 语音模型
├── tools/                 # 第三方工具 + 测试（ffmpeg / ffprobe / tests）
├── subtitle_app/          # 源码包（含入口 subtitle_app.py）
└── 启动字幕工具.bat       # 快速启动
```

## 测试

```bash
python -m unittest discover -s tools/tests
```

## 注意事项

- 模型需手动从 modelscope 下载（约 1.6 GB），放入 `models/faster-whisper-large-v3-turbo/`
- 翻译缓存文件 `cache/.subtitle_translation_cache.json` 超过 10000 条时会自动裁剪

## 技术栈

- PySide6（Qt 桌面 GUI）
- faster-whisper（本地语音识别）
- ffmpeg（音频提取 / 字幕内嵌）
- OpenAI Chat Completions API（翻译接口）
