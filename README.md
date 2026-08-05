# 🎬 本地字幕生成与双语翻译工具

基于 **faster-whisper** 的本地语音转写 + **AI** 翻译的 Windows 桌面工具。拖拽视频/音频即可一键生成双语字幕，支持批量处理、并行流水线和 MKV 字幕内嵌。

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-blue" alt="Python">
  <img src="https://img.shields.io/badge/Platform-Windows-blue" alt="Platform">
  <img src="https://img.shields.io/badge/License-MIT-green" alt="License">
</p>

---

## ✨ 功能亮点

| 功能 | 说明 |
|------|------|
| **🎤 语音转写** | 本地 faster-whisper-large-v3-turbo 模型，支持 CUDA 加速，离线运行 |
| **🌍 AI 翻译** | 兼容 OpenAI Chat Completions API（DeepSeek / 商汤 / 任意自建接口），批量翻译 + 句子级缓存去重 |
| **📝 双语字幕** | 原文 + 译文上下对照排列，自动繁简转换 |
| **📦 MKV 内嵌** | 自动将字幕软内嵌到 MKV 视频文件中，播放器直出双语字幕 |
| **⏯ 断点续转/续翻** | 程序崩溃或手动中断后可从中断处继续，无需重新处理 |
| **⚡ 并行流水线** | 转写与翻译并行执行，充分利用 GPU + CPU 资源，批量效率翻倍 |
| **📊 实时进度** | 实时进度条 + ETA 估算 + 文件级状态追踪 |
| **🎯 拖拽操作** | 支持文件/文件夹拖拽到列表，支持内部拖拽排序 |
| **🔍 字幕预览** | 内嵌字幕预览面板，支持查找、时间偏移微调、手动编辑 |
| **🛡 质量检查** | 自动检测字幕空行、过短/过长、时间重叠、间隙过长等问题并生成报告 |
| **💾 翻译缓存** | 句子级缓存避免重复翻译，可手动查看/清理缓存 |
| **🗂 多方案管理** | 支持配置多个翻译 API 方案，一键切换 |

---

## 📋 系统要求

- **操作系统**：Windows 10/11
- **Python**：3.10+（[下载地址](https://www.python.org/downloads/windows/)，安装时务必勾选 **Add Python to PATH**）
- **GPU**（强烈推荐）：NVIDIA 显卡 + [CUDA 12.x](https://developer.nvidia.com/cuda-downloads) + [cuDNN](https://developer.nvidia.com/cudnn)；纯 CPU 可用但转写速度慢约 10 倍
- **Visual C++ Redistributable**：[下载链接](https://aka.ms/vs/17/release/vc_redist.x64.exe)（PySide6 必需，缺少则启动闪退）

---

## 🚀 快速开始

### 1️⃣ 下载项目

```bash
git clone https://github.com/Eric0610-2026/subtitle-tool.git
cd subtitle-tool
```

### 2️⃣ 安装依赖

```bash
# 如需 GPU 加速，先安装 CUDA 版 PyTorch（可选，强烈推荐）
pip install torch --index-url https://download.pytorch.org/whl/cu121

# 安装全部依赖
pip install -r tools/requirements.txt
```

### 3️⃣ 准备 ffmpeg（必需）

应用按以下顺序查找 ffmpeg/ffprobe：**应用目录 → `tools/` 目录 → 系统 PATH**（`shutil.which`），任一处找到即用。

**方式一：使用系统已安装的 ffmpeg（推荐）**

如果电脑已安装 ffmpeg（含 ffprobe）并已加入 PATH（命令行执行 `ffmpeg -version` 有输出即满足），无需任何额外操作。

**方式二：放入 `tools/` 目录**

从 [ffmpeg.org](https://ffmpeg.org/download.html) 下载 Windows 完整构建，将 `bin/` 目录下的 **ffmpeg.exe** 和 **ffprobe.exe** 放入项目 `tools/` 目录中。**两个文件缺一不可。**

### 4️⃣ 下载语音识别模型（必需，约 1.6 GB）

从 [ModelScope](https://www.modelscope.cn/models/pengzhendong/faster-whisper-large-v3-turbo/summary) 下载整个 `faster-whisper-large-v3-turbo` 文件夹（包含 `model.bin`、`tokenizer.json`、`vocabulary.json`、`config.json` 等），放入 `models/faster-whisper-large-v3-turbo/` 目录。

项目已预置模型路径配置，放入即可使用。

### 5️⃣ 配置 API 密钥

```bash
# 复制配置模板
cp subtitle_app/config.example.json subtitle_app/config.json
```

编辑 `subtitle_app/config.json`，配置翻译接口：

```json
{
  "translation": {
    "api_url": "https://api.deepseek.com/v1/chat/completions",
    "api_key": "sk-你的密钥",
    "model": "deepseek-chat",
    "target_lang": "zh"
  }
}
```

支持多个翻译方案并存，可在应用内一键切换（设置 > 翻译方案）。

### 6️⃣ 启动应用

```bash
python subtitle_app/subtitle_app.py
```

> **💡 首次启动较慢**：PySide6 和 torch 加载需要 20-30 秒，界面不会立即弹出，请耐心等待。

> **💡 首次运行自动装依赖**：入口脚本会自动检测并安装 `requirements.txt` 中的依赖，首次启动时会有命令行弹窗显示安装进度。

---

## 🎯 使用指南

### 基本流程

1. **添加文件**：拖拽视频/音频到「视频/音频生成字幕」标签页，或拖拽已有 `.srt` 文件到「已有字幕翻译」标签页
2. **开启翻译**：勾选「🌍 开启 AI 翻译」复选框
3. **点击「▶ 开始处理」**：程序自动执行语音转写 → AI 翻译 → 字幕落盘 → 可选 MKV 内嵌
4. **查看结果**：每个视频目录下生成 SRT 字幕文件（翻译完成后如配置了 MKV 内嵌，还会自动生成内嵌字幕的视频）

### 输出文件说明

处理完成后，输出目录（与输入文件同目录或同名子目录）中包含：

| 文件 | 说明 |
|------|------|
| `视频名.srt` | **最终字幕**（纯译文或双语，取决于配置） |
| `视频名.mkv` | 内嵌字幕的视频文件（如勾选 MKV 内嵌） |
| `*.translate_state.json` | 断点续翻状态文件（处理完成后自动清理） |

### 功能详解

- **翻译开关**：不勾选翻译时，仅执行语音转写，生成原文 SRT
- **MKV 内嵌**：在「更多设置」中可开启「嵌入前暂停」，在弹窗中预览/编辑字幕后再决定是否嵌入
- **提取内嵌字幕**：点击「📤 提取字幕」按钮，选择含内嵌字幕的视频文件（如 MKV），批量提取其第一个字幕流为独立 SRT；图像字幕（PGS/VobSub 等）无法提取为文本 SRT。勾选「提取后转为 MP4」可同时将视频转换为 MP4（不带字幕，流复制优先、失败自动降级重编码），时长验证通过后删除原文件
- **并行流水线**：在配置文件的 `translation.concurrency_pipeline` 中设置并行度（默认 2），数值越高批量处理越快
- **断点续翻**：如处理过程中断，重新添加相同文件会自动检测未完成状态并从中断处继续

---

## ⚙ 配置文件

所有参数集中在 `subtitle_app/config.json`（从 `config.example.json` 复制创建）。

**推荐做法（无需手改 JSON）**：复制 `config.example.json` 为 `config.json` 后，启动应用，在「⚙ 更多设置」对话框中完成个性化配置——默认视频目录、模型目录、识别语言、翻译方案/API 密钥、语言检测复用、字幕备份份数等，点击「💾 永久保存」自动写回 `config.json`（点击「💾 本次有效」则仅对当前会话生效）。多数修改无需重启；若需回退，直接删除 `config.json` 即可恢复默认。

### 核心配置项

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `whisper.model_dir` | 语音模型路径 | `models/faster-whisper-large-v3-turbo` |
| `whisper.device` | 计算设备 | `cuda`（可选 `cpu`） |
| `whisper.language` | 识别语言 | `auto`（自动检测） |
| `whisper.compute_type` | 计算精度 | `int8_float16` |
| `whisper.vad_filter` | VAD 语音活动检测过滤 | `true` |
| `whisper.reuse_auto_lang` | auto 模式下复用同批首个检测语言（混合语言目录请保持关闭） | `true` |
| `translation.api_url` | 翻译 API 地址 | — |
| `translation.api_key` | 翻译 API 密钥 | — |
| `translation.model` | 翻译模型名称 | — |
| `translation.target_lang` | 目标语言 | `zh` |
| `translation.batch_size` | 翻译批处理大小 | `50` |
| `translation.pipeline` | 启用并行流水线 | `true` |
| `translation.concurrency_pipeline` | 并行流水线视频并发数 | `2` |
| `translation.backup_max_files` | `logs/srt_backup` 字幕备份保留份数（超出自动清理最旧） | `50` |
| `translation.presets` | 多翻译方案配置 | `[]` |
| `app.default_video_dir` | 默认视频目录（可在「更多设置」中填写并永久保存） | — |
| `theme.light` / `theme.dark` | 浅色/深色主题颜色 | 内置配色 |

---

## 📂 项目结构

```
├── subtitle_app/                  # 主应用包
│   ├── subtitle_app.py            # 入口：自动安装依赖后启动 Qt 界面
│   ├── qt_app.py                  # PySide6 主窗口 UI + 事件处理
│   ├── panels.py                  # 进度条、字幕预览、日志面板
│   ├── widgets.py                 # 拖放文件列表、日志条目等自定义控件
│   ├── dialogs.py                 # 设置、历史记录、缓存管理对话框
│   ├── transcriber.py             # 音频提取 + faster-whisper 语音转写
│   ├── translation.py             # AI 翻译客户端（缓存、批量、断点续翻）
│   ├── translator.py              # 翻译阶段编排（翻译 → 落盘 → MKV 内嵌）
│   ├── pipeline.py                # 并行流水线编排（转写→翻译 多线程）
│   ├── srt_utils.py               # SRT 解析/写入、进度跟踪、繁简转换
│   ├── muxer.py                   # MKV 字幕软内嵌、SRT 提取
│   ├── config.py                  # JSON 配置加载模块
│   ├── config.example.json        # 配置模板（不含密钥）
│   └── config.json                # 本地配置（含 API 密钥，不提交到仓库）
├── tools/                         # 第三方工具与测试
│   ├── ffmpeg.exe / ffprobe.exe   # 音视频处理（可选：可改用系统 PATH 版）
│   ├── requirements.txt           # Python 依赖清单
│   └── tests/                     # 单元测试（pipeline / srt_utils / translation 等）
│       ├── test_pipeline.py
│       ├── test_srt_utils.py
│       ├── test_transcriber.py
│       ├── test_translation.py
│       ├── test_translator.py
│       ├── test_muxer.py
│       └── test_widgets.py
├── models/                        # Whisper 语音模型文件
│   └── faster-whisper-large-v3-turbo/
├── cache/                         # 运行时缓存（可自动清理）
│   ├── .subtitle_translation_cache.json
│   └── .subtitle_ignore.json
├── logs/                          # 运行日志 + SRT 备份
│   └── subtitle_tool.log
└── docs/
    └── README.md                  # 本文档
```

---

## 🧪 运行测试

```bash
# 运行所有测试
python -m unittest discover -s tools/tests -v

# 运行单个测试文件
python -m unittest tools.tests.test_translator -v
python -m unittest tools.tests.test_muxer -v
python -m unittest tools.tests.test_widgets -v
```

---

## ⚠ 注意事项

- **模型文件较大**（~1.6 GB），需手动从 ModelScope 下载，不支持自动拉取
- **翻译缓存** `cache/.subtitle_translation_cache.json` 超过 10,000 条时会自动裁剪最旧条目
- **API 密钥安全**：`config.json` 已加入 `.gitignore`，不会提交到仓库；首次使用请从 `config.example.json` 复制创建
- **高峰时段提醒**：使用 DeepSeek API 时，北京时间 9:00-12:00、14:00-18:00 为高峰时段，应用会弹出价格提醒

---

## 🧰 技术栈

| 组件 | 用途 |
|------|------|
| [PySide6](https://doc.qt.io/qtforpython/) | 桌面 GUI 框架 |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | 本地语音识别（CTranslate2 加速） |
| [ffmpeg](https://ffmpeg.org/) | 音频提取、字幕内嵌（MP4 → MKV 重封装） |
| OpenAI Chat Completions API | AI 翻译接口（兼容 DeepSeek / 商汤 / 任意 API） |
| [opencc-python](https://github.com/yichen0831/opencc-python-reimplemented) | 繁简体中文转换（可选，有内置降级表） |

---

> 有问题或建议？欢迎提交 [Issue](https://github.com/Eric0610-2026/subtitle-tool/issues) 或 PR。
