# 项目指南

## 入口与运行

- 入口：`subtitle_app.py:1` → `subtitle_app.qt_app.main()`
- 运行：双击 `字幕工具.lnk`（最快）或 `python subtitle_app.py`
- `subtitle_app.py` 智能检测：首次运行自动 `pip install -r requirements.txt` 并创建 `.deps_installed` 标记，此后跳过
- 仅 Windows（`os.startfile`、ffmpeg 二进制）

## 测试

```powershell
python -m unittest discover -s tests          # 全部
python -m unittest tests.test_srt_utils        # 单文件
python -m unittest tests.test_srt_utils.TestSrtRoundtrip  # 单用例
```

- 框架：`unittest`（无 pytest）
- 当前 48 例，分布在 `test_srt_utils.py`（22）和 `test_translation.py`（26）
- 无需网络或模型加载；API 调用用 `unittest.mock`

## 项目结构

`subtitle_app/` 下 10 个源模块（不含 `__init__.py`）：

| 模块 | 职责 |
|---|---|
| `qt_app.py` | Qt 主窗口 UI、事件分发、主题、预览 |
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

## 配置与安全

- 配置在 `subtitle_app/config.json`，从 `config.example.json` 复制创建
- **API 密钥明文**在 `config.json:api_key`，已加入 `.gitignore`，切勿提交
- `config.py` fallback：优先读 `config.json`，不存在自动回退 `config.example.json`
- 改配置后需**重启应用**（各模块导入时固化 `cfg`，无热重载）
- `config.json`、模型目录、ffmpeg 均已 git-ignored

## 代码惯例

- 文件头：`#!/usr/bin/env python3` + `# -*- coding: utf-8 -*-`
- 类型标注：推荐用但非强制——部分函数（`transcriber.py:75`）尚未标注
- imports 风格：代码实际用 `from typing import List`（非 `list[str]`）
- logging：多数模块有 `logger = logging.getLogger(__name__)`，但 `config.py` 和 `dialogs.py` 无
- 字符串双引号、4 空格缩进

## 给 AI agent 的提示

- Windows 搜索用 `Select-String`（`grep`/`rg` 不可用）
- 10 个模块各司其职，避免引入新抽象
- 改完运行全部 48 例测试
- 不改 `config.json`；改配置模板应改 `config.example.json`
