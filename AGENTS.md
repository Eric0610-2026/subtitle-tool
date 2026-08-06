# 项目指南（AGENTS.md）

本文件是 AI agent 的项目手册：**放在项目根目录，每次新会话自动加载**，
AI 无需重新通读代码即可回答架构/测试/惯例问题。修改核心架构后请同步更新本文件。

## 入口与运行

- 入口：`subtitle_app/subtitle_app.py` → `subtitle_app.qt_app.main()`
- 运行：双击 `字幕工具.lnk` 或 `python subtitle_app/subtitle_app.py`
- `subtitle_app.py` 首次运行自动 `pip install -r tools/requirements.txt`（超时 300s），成功则创建 `cache/.deps_installed` 标记
- 仅 Windows：ffmpeg/ffprobe 查找顺序 = 应用目录 → `tools/` → 系统 PATH（`srt_utils.find_tool`）

## 测试

```powershell
python -m unittest discover -s tools/tests      # 全部（173 例）
python -m unittest tools.tests.test_translator   # 单文件
python -m unittest tools.tests.test_translator.TestBatchSizePersistenceField  # 单用例
```

- 框架：`unittest`（无 pytest）；无需网络或模型，API 调用用 `unittest.mock`
- 8 个测试文件（`tools/tests/`）：test_srt_utils / test_translation / test_translator /
  test_transcriber / test_pipeline / test_muxer / test_widgets / test_local_service
- 改完必须跑全量：`python -m unittest discover -s tools/tests`

## 模块清单（subtitle_app/）

| 模块 | 职责 |
|---|---|
| `subtitle_app.py` | 入口：自动装依赖后启动 Qt |
| `qt_app.py` | Qt 主窗口 UI、事件分发、设置保存（含 `_batch_size_save_field`） |
| `panels.py` | 进度/预览/日志面板 + `SignalBridge`（Qt Signal 跨线程回传事件） |
| `widgets.py` | `DropListWidget`（拖放列表）、`LogEntry`、`SCAN_VIDEO_EXTS` |
| `dialogs.py` | 设置、模型配置、历史、缓存、嵌入对话框 |
| `theme.py` | 明暗主题配色、QSS |
| `notifier.py` | winotify → PowerShell 系统通知（白名单防注入） |
| `transcriber.py` | ffmpeg 提取音频 + faster-whisper 转写、断点 `.partial.srt` |
| `translation.py` | 翻译客户端：句子级缓存、批处理、递归降级、403 curl fallback |
| `translator.py` | 翻译阶段编排：翻译→组装双语→落盘→MKV 内嵌→备份 |
| `pipeline.py` | 串行/并行流水线编排、`_DaemonThreadPoolExecutor`、停止管理 |
| `srt_utils.py` | SRT 解析/写入、断句、繁简转换、`OverallProgress`、质量检查 |
| `muxer.py` | MKV 软内嵌、从视频提取内嵌字幕、转 MP4（ffprobe 探测 + ffmpeg，时长验证后删除原文件；MP4 源带内嵌字幕时去字幕重封装替换原文件） |
| `local_service.py` | 本地 Hy-MT2 llama-server 自动拉起/探测/退出清理 |
| `config.py` | 读取 config.json → `SimpleNamespace` 单例 `cfg` |

## 架构要点

```
媒体文件 → pipeline._transcribe_stage → {video}.{lang}.srt
         → pipeline._translate_stage → translator.translate_only → muxer 或外挂 SRT
```

- **并行模式（concurrency≥2）= GPU 单模型两阶段**（`pipeline._run_staged`）：
  阶段 1 全部转写（仅驻留 Whisper）→ `release_model()` 释放显存 →
  阶段 2 用 `_DaemonThreadPoolExecutor` 并行翻译+自动内嵌（仅驻留 llama-server）。
  串行模式（concurrency=1）走 `_process_one` 逐文件。
- **跨线程回传**：worker 线程 post 事件 dict → `SignalBridge`（Qt Signal，队列连接）→
  主线程 `_handle_event` 按类型分发（`_event_handlers` 只构建一次）。
- **翻译**：`translation.py` 内 `ThreadPoolExecutor` 并发批量调 API；
  进程级共享缓存 `_shared_cache` + 全局锁防并发写盘覆盖；句子级去重。
  段落上下文用「future 链」实现：worker 内等本段前一批完成再带 `（上文）…` 提交，
  段内严格有序、段间并行（`para_gate`/`para_context`）。
- **批大小**：本地模式读 `cfg.translation.batch_size`，联网模式读
  `cfg.translation.batch_size_online`；永久保存时 `_batch_size_save_field` 按 mode 写对应字段。
- **断点续转/续翻**：`.partial.srt`（每 30 段）+ `*.translate_state.json`；
  `cache/.subtitle_ignore.json` 记录已完成文件。
- **数据净化**：转写后 `sanitize_blocks()` + 内嵌前 `_sanitize_srt_for_mux()` 双重校验。
- **嵌入前暂停**：`translator.py` 中 `PauseResponse`（event + action + modified_text）。

## 配置与安全

- 配置在 `subtitle_app/config.json`（从 `config.example.json` 复制创建）；改模板应改 example
- **API 密钥明文在 config.json，已 git-ignore，切勿提交**；改配置后需重启应用（`cfg` 导入时固化）
- `.gitignore`：config.json、models/、tools/ffmpeg*.exe、cache/、logs/、.reasonix/、reasonix.toml

## 代码惯例

- 文件头 `#!/usr/bin/env python3` + `# -*- coding: utf-8 -*-`；`logger = logging.getLogger(__name__)`
- 类型标注用 `typing.List/Dict/Optional`（少数文件用 PEP 585/604 原生泛型）
- 字符串双引号主导、4 空格缩进；避免引入新抽象，各模块职责单一

## 给 AI agent 的提示（省 token）

- 本文件已含架构全貌，**优先读它**；细节用 `grep`/`code_index`/LSP 精准定位，**不要整读大文件**
  （qt_app.py / dialogs.py / panels.py 均 30KB+）
- 探索型问题（"X 如何工作""找所有 Y"）用 **explore 子代理**：它隔离读取，只回蒸馏结论
- Windows + PowerShell 环境：路径用 `\`，多命令用 `;` 连接
- 改完跑全量测试；测试新增放 `tools/tests/test_*.py`
- 核心改动（新增模块/改数据流/改配置结构）后更新本文件与 README
