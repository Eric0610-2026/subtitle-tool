# 项目指南

## 入口与运行

- 入口：`subtitle_app/subtitle_app.py` → `subtitle_app.qt_app.main()`
- 运行：双击 `字幕工具.lnk` 或 `python subtitle_app/subtitle_app.py`
- `subtitle_app.py` 智能检测：首次运行自动 `pip install -r tools/requirements.txt`（超时 300s），成功则创建 `cache/.deps_installed` 标记，此后跳过
- 仅 Windows（ctypes MessageBoxW、ffmpeg 二进制）

## 测试

```powershell
python -m unittest discover -s tools/tests          # 全部（129 例）
python -m unittest tools.tests.test_srt_utils        # 单文件
python -m unittest tools.tests.test_srt_utils.TestSrtRoundtrip  # 单用例
```

- 框架：`unittest`（无 pytest）
- 7 个测试文件：`test_srt_utils.py`、`test_translation.py`、`test_translator.py`、`test_transcriber.py`、`test_pipeline.py`、`test_muxer.py`、`test_widgets.py`（位于 `tools/tests/`）
- 当前 129 例
- 无需网络或模型加载；API 调用用 `unittest.mock`
- 运行全部测试：`python -m unittest discover -s tools/tests`

## 项目结构

`subtitle_app/` 下 11 个源模块（不含 `__init__.py`）：

| 模块 | 职责 |
|---|---|
| `qt_app.py` | Qt 主窗口 UI、事件分发、主题、预览 |
| `panels.py` | 进度/预览/日志面板 UI 组件 |
| `widgets.py` | `DropListWidget`（拖放列表）、`LogEntry`（日志条目） |
| `transcriber.py` | 音频提取 + faster-whisper 转写 |
| `translation.py` | AI 翻译客户端、缓存、批处理、curl fallback |
| `translator.py` | 翻译阶段编排（消费转写→翻译→组装） |
| `pipeline.py` | 串行/并行流水线编排、子进程管理、停止 |
| `srt_utils.py` | SRT 解析/写入、断句、繁简转换、进度跟踪 |
| `muxer.py` | MKV 软内嵌、.ts 修复、时长验证 |
| `dialogs.py` | 设置、历史、缓存管理对话框 |
| `config.py` | 读取 JSON 配置，返回 `SimpleNamespace` 单例 `cfg` |

## 架构要点

```
媒体文件 → Transcriber → {video}.{lang}.srt → translator.py → muxer.py (MKV) 或外挂 SRT
```

- **线程**：Qt 主线程（UI）+ 工作线程（转写）+ 队列（翻译消费），经 `ui_queue`（maxsize=2000）回传
- **并发**：串行（`concurrency=1`）或并行（≥2：转写 N+1 与翻译 N 并行）
- **翻译加速**：`translation.py` 内部用 `ThreadPoolExecutor`（`concurrency_translate`，默认 3 线程）并行发送 API batch；`pipeline.py` 多文件并行翻译（同一线程池）
- **断点续转/续翻**：`.partial.srt`（每 30 段写一次）+ `*.translate_state.json`
- **数据净化**：转写后 `sanitize_blocks()` + 内嵌前 `_sanitize_srt_for_mux()` 双重校验
- **嵌入前暂停**：`translator.py` 中 `PauseResponse` 机制，支持用户预览/编辑字幕后再嵌入

## 配置与安全

- 配置在 `subtitle_app/config.json`，从 `config.example.json` 复制创建
- **API 密钥明文**在 `config.json:api_key`，已加入 `.gitignore`，切勿提交
- `config.py` fallback：优先读 `config.json`，不存在自动回退 `config.example.json`
- 改配置后需**重启应用**（各模块导入时固化 `cfg`，无热重载）
- `.gitignore` 覆盖：`config.json`、`models/`（大模型）、`tools/ffmpeg.exe`/`tools/ffprobe.exe`、`cache/`、`logs/`、`reasonix.toml`、`.reasonix/`

## 代码惯例

- 文件头：全部 13 个 `.py` 文件均有 `#!/usr/bin/env python3` + `# -*- coding: utf-8 -*-`
- 类型标注：主流用 `from typing import List, Dict, Optional`（PEP 585/604 原生泛型偶见于 `config.py`、`muxer.py`、`srt_utils.py`）
- logging：9/13 文件有 `logger = logging.getLogger(__name__)`；`config.py`、`dialogs.py`、`subtitle_app.py`、`__init__.py` 无（均属合理）
- 字符串双引号占绝对主导（>95%）、4 空格缩进

## 给 AI agent 的提示

- Windows：搜索用 `Select-String`（`grep`/`rg` 不可用），Powershell 命令用分号 `;` 而非 `&&`
- 12 个 `.py` 文件（含入口 `subtitle_app.py`）各司其职，避免引入新抽象
- 改完运行全部测试 `python -m unittest discover -s tools/tests`
- 不改 `config.json`；改配置模板应改 `config.example.json`
- `reasonix.toml` 是本地配置，**不提交到 GitHub**（已 git-ignored 且已从追踪中移除）
