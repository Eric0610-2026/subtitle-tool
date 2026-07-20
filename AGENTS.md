# AGENTS.md

字幕生成与双语翻译工具（`zimu_app` 包）。Windows 桌面 GUI（PySide6），本地 faster-whisper 转写 + OpenAI 兼容接口翻译。

## 运行与入口
- 入口：`subtitle_app.py:1` → `zimu_app.qt_app:main`。双击 `启动字幕工具.bat` 以 `pythonw.exe` 无控制台启动。
- **仅 Windows**：`os.startfile`、注册表读主题、应用目录内的 `ffmpeg.exe`/`ffprobe.exe`。
- 应用目录需存在 `ffmpeg.exe`、`ffprobe.exe`、`faster-whisper-large-v3-turbo/model.bin` 才能转写。

## 模块（10 个源文件）

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `qt_app.py` | 1229 | Qt 主窗口 UI、事件、主题、预览/编辑、日志、手动嵌入 |
| `srt_utils.py` | 574 | SRT 解析/写入/净化、繁简转换、JSON 原子读写、**进度跟踪**、**`find_tool`** |
| `transcriber.py` | 479 | 音频提取 + Whisper 转写、模型缓存、**断点续转** |
| `muxer.py` | 387 | MKV 软内嵌、.ts 修复、时长验证、降级重试 |
| `translation.py` | 416 | AI 翻译客户端、缓存、批量、403 curl fallback、递归容错 |
| `pipeline.py` | 323 | 串行/并行流水线编排、子进程管理、停止 |
| `dialogs.py` | 300 | 设置/历史/缓存管理对话框 |
| `translator.py` | 222 | 翻译阶段编排（消费转写→翻译→组装输出） |
| `config.py` | 43 | 读取 `config.json` 转为 `SimpleNamespace`（模块级 `cfg` 单例） |
| `config.json` | 103 | 全部参数集中仓库 |

## 配置陷阱（重要）
- **修改 `config.json` 后必须重启进程**——各模块在导入时读取 cfg 固化为模块级常量（`translation.py:28-34`、`srt_utils.py:24-31`），热重载无效。
- `config.json` 中 `translation.api_key` 为**明文**，仓库**无 `.gitignore`**——切勿提交密钥。

## 测试
- 纯 unittest，无 pytest：`python -m unittest discover -s tests`（从仓库根目录）。
- 2 个文件：`test_srt_utils.py`（22 例）、`test_translation.py`（26 例），共 48 例。
- 无 lint / typecheck / CI 配置。

## 架构要点

### 数据流
```
媒体文件 → Transcriber → {视频}.{lang}.srt → TranslationClient → translator.py → muxer.py(MKV) 或 外挂 SRT
```

### 线程模型
- 主线程 Qt UI + 工作线程（转写）+ 队列（翻译消费），经有界 `ui_queue`（maxsize=2000）回传事件。
- 串行（concurrency=1）或并行流水线（≥2：转写 N+1 与翻译 N 并行）。

### 翻译接口
- 标准 `urllib` 调 OpenAI Chat Completions 兼容接口。
- HTTP 403 → `ApiForbiddenError` → 自动 `curl` fallback（无 shell）。
- SHA256 句子缓存（`.subtitle_translation_cache.json`，FIFO 裁剪 10000→保留最新 5000）。
- 断点续翻（`*.translate_state.json`），完成后自动删除。
- 递归深度保护（`MAX_RECURSION_DEPTH=5`），超限返回原文；单句失败走纯文本兜底。

### 断点续转（transcriber.py）
- 每 `checkpoint_interval`（默认 30）段原子写入 `.partial.srt`。
- 重启检测 `.partial.srt` → 从断点裁剪音频 → 传递最后 5 句为 `initial_prompt`。

### MKV 内嵌（muxer.py）
1. SRT 时间戳净化 → 2. .ts 修复性重封装 → 3. ffmpeg 软内嵌 → 4. **时长验证**（<95% 不通过）→ 5. 降级重试
- 内嵌成功+时长可信 → 删原视频+临时文件；时长达标→回退外挂 SRT 并保留原视频；失败→回退外挂 SRT。

### 数据净化双重保障
- **转写端**：`transcriber.py` 写出前调用 `sanitize_blocks()`（start≥0、时间单调、end>start 至少 0.5s）。
- **内嵌端**：`muxer.py` 内嵌前 `_sanitize_srt_for_mux()` 兜底（仅时间戳异常时生成临时净化文件）。

### 智能进度权重
- `OverallProgress._file_weights` 支持 per-file 覆盖 `transcribe_weight`。
- 纯翻译文件 `transcribe_weight=0`，进度 100% 由翻译驱动。

### 设置持久化
- 窗口尺寸/位置 → `~/.subtitle_tool_settings.json`。
- 参数持久化需改 `config.json` 后重启；对话框修改仅本次有效。

## 代码精简记录（2026-07）
- `progress.py` 合并到 `srt_utils.py`（-1 文件）。
- `_SIMPLE_T2S` 去重 ~20 键值对。
- `muxer.py`：4 清理函数合并为 `_cleanup_file`。
- `qt_app.py`：`_make_btn`（14 按钮统一）、`_build_ui` 拆分（240→120 行）、`_handle_event` 分发表（99 行 if-elif 链→dispatch）、`_confirm`/`_load_done_set`/`log` 内联等。
- `find_tool` 提取到 `srt_utils.py`，消除与 `pipeline.py` 的重复。
- 48 测试全通过，零功能变更。
