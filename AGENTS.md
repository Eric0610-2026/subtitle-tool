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
python -m unittest discover -s tools/tests      # 全部（141 例）
python -m unittest tools.tests.test_translator   # 单文件
python -m unittest tools.tests.test_translator.TestBatchSizePersistenceField  # 单用例
```

- 框架：`unittest`（无 pytest）；无需网络或模型，API 调用用 `unittest.mock`
- 9 个测试文件（`tools/tests/`）：test_srt_utils / test_translation / test_translator /
  test_transcriber / test_pipeline / test_muxer / test_widgets / test_local_service / test_dialogs
- 改完必须跑全量：`python -m unittest discover -s tools/tests`

## 模块清单（subtitle_app/）

| 模块 | 职责 |
|---|---|
| `subtitle_app.py` | 入口：自动装依赖后启动 Qt |
| `qt_app.py` | Qt 主窗口 UI、事件分发、设置保存（含 `_batch_size_save_field`） |
| `panels.py` | 进度/预览/日志面板 + `SignalBridge`（Qt Signal 跨线程回传事件） |
| `widgets.py` | `DropListWidget`（拖放列表）、`LogEntry`、`SCAN_VIDEO_EXTS` |
| `dialogs.py` | 设置、历史、缓存、嵌入对话框 |
| `theme.py` | 明暗主题配色、QSS |
| `notifier.py` | winotify → PowerShell 系统通知（白名单防注入） |
| `transcriber.py` | ffmpeg 提取音频 + faster-whisper 转写、断点 `.partial.srt` |
| `translation.py` | 翻译客户端：句子级缓存、批处理、递归降级、停止与熔断 |
| `translator.py` | 翻译阶段编排：翻译→组装双语→落盘→MKV 内嵌→备份 |
| `pipeline.py` | 两阶段流水线编排（转写→翻译 顺序）、停止管理 |
| `srt_utils.py` | SRT 解析/写入、断句、繁简转换、`OverallProgress` |
| `muxer.py` | MKV 软内嵌、从视频提取内嵌字幕、转 MP4（ffprobe 探测 + ffmpeg，时长验证后删除原文件；MP4 源带内嵌字幕时去字幕重封装替换原文件） |
| `local_service.py` | 本地 Hy-MT2 llama-server 自动拉起/探测/退出清理（端口 `_PORT=8188`，不用 8080 是因为它常被 Hyper-V/WSL 动态预留；`service_url_prefix()` 供各模块识别"本地服务 URL"；启动日志落盘 `cache/.llama-server.log`，启动失败时读日志尾部给出真实原因，如端口被 Windows 预留时提示 netsh 修复命令） |
| `config.py` | 读取 config.json → `SimpleNamespace` 单例 `cfg` |
| `handoff.py` | 仅监听 `127.0.0.1:49732` 的下载器导入协议，校验请求格式 |

## 架构要点

```
媒体文件 → pipeline._transcribe_stage → {video}.{lang}.srt
         → pipeline._translate_stage → translator.translate_only → muxer 或外挂 SRT
```

- **两阶段调度**（`pipeline._run_staged`，`_process_one` 已删除）：
  阶段 1 全部转写（仅驻留 Whisper）→ `release_model()` 释放显存 →
  阶段 2 顺序翻译+自动内嵌（仅驻留 llama-server）。
  翻译走本地 llama-server（`--parallel 1`），文件级/批次级均为串行：
  多路并发只会排队超时，模型各只加载一次；`pause_before_embed` 逐文件生效。
- **跨线程回传**：worker 线程 post 事件 dict → `SignalBridge`（Qt Signal，队列连接）→
  主线程 `_handle_event` 按类型分发（`_event_handlers` 只构建一次）。
- **翻译**：`translation.py` 批量调本地 API；
  进程级共享缓存 `_shared_cache` + 全局锁防并发写盘覆盖（缓存对话框删除/清空必须
  走 `remove_shared_cache_entries`/`clear_shared_cache` 同步内存，否则条目复活）；
  缓存淘汰按 LRU（命中即触碰，`MAX_CACHE_ENTRIES` 超额裁掉一半最久未用）；
  句子级去重。
  段落上下文用「future 链」实现：worker 内等本段前一批完成再带 `（上文）…` 提交，
  段内严格有序（`para_gate`/`para_context`，默认批次并发 1）。
  **停止与熔断**：`translate_blocks(stop_check=...)` 检测用户停止抛 `TranslationStopped`
  （translator 静默返回，不算失败）；网络类错误抛 `ApiUnavailableError` 不拆批；
  连续 `MAX_EMPTY_BATCHES`(3) 空批判定 API 不可用中止本文件；补翻连续 20 句无进展放弃；
  异常/停止时 `_abort_translation` 落盘断点并取消未开始的批次。
- **任务配置快照**：`qt_app._build_opts` 将设置对话框中可修改的任务参数（含批大小、
  备份份数）解析为具体值；`SubtitleWorker.start` 随即复制并冻结该 dict。后台阶段不得
  回读 `cfg` 覆盖这些值，设置改动只影响下一次启动的任务。
- **断点续转/续翻**：`.partial.srt`（每 30 段）+ `*.translate_state.json`；
  `cache/.subtitle_ignore.json` 记录已完成文件；`save_json` 对 Windows 并发
  replace 冲突做短暂重试。
- **数据净化**：转写后 `sanitize_blocks()` + 内嵌前 `_sanitize_srt_for_mux()` 双重校验。
- **嵌入前暂停**：`translator.py` 中 `PauseResponse`（event + action + modified_text）。
- **事件契约**：`done` 事件可带 `stopped: True`（用户停止，UI 不再谎报"全部完成"）；
  失败按文件粒度处理（`log` ERROR + 跳过继续），全部失败才发 `error`，部分失败发 `done` 带失败数。
  `counter` 事件由 pipeline 维护真实完成计数（`_bump_counter`/`_post_counter`，
  文件级并发下文件序号≠完成数，禁止再用 idx 当累计值）。
- **输入防御**：`_run` 开头检测同目录同名不同格式媒体文件（同 stem 冲突，大小写不敏感）→ 报错中止；
  `find_existing_subtitle` 忽略 `.partial.srt`；断点续翻按 stem 前缀匹配，防止串用别的视频的状态文件。
- **下载器联动**：`qt_app` 通过 `QTcpServer` 只监听回环地址；收到媒体路径后仅接受现存的支持格式，加入视频队列、自动跳过重复/已处理项并激活现有窗口，绝不自动开始处理。
- **托盘常驻**：可用时创建 `QSystemTrayIcon`，普通 `closeEvent` 仅保存窗口状态并隐藏，不设 `_closing` 或停止 worker；单击图标恢复，右键菜单可打开或彻底退出。退出时若有运行中任务先确认，再走原关闭清理。隐藏时嵌入确认事件排队并发通知，恢复窗口后再显示对话框；系统通知区域不可用时正常关闭。
- **配置钳位**：`checkpoint_interval`/`batch_size` 读取处 `max(1, int(...) or 默认)`，杜绝 0 值崩溃。
- **预览渲染**：`PreviewPanel` 实时追加 200ms 合并渲染（QTimer 单次触发），
  只渲染最近 300 块（`_visible_block_slice`，块索引带偏移映射回 `_raw_text` 全文供编辑回写）。

## 配置与安全

- 配置在 `subtitle_app/config.json`（从 `config.example.json` 复制创建）；改模板应改 example
- **配置生效规则**：点击「本次有效」会更新当前会话，之后新启动的任务使用该设置；点击
  「永久保存（下次默认）」还会写入 `config.json`，供下次启动作为默认值。正在运行的任务
  永不改变；直接编辑 `config.json` 后请重启应用。
- **翻译为纯本地方案，不存在联网 API 能力**：翻译端点由
  `local_service.translation_endpoint()` 单点定义（`127.0.0.1:8188`），
  `TranslationClient` 不接受地址/密钥参数，配置中也没有 `api_url`/`api_key` 字段。
  **禁止**重新引入可配置的外部 API 地址、密钥字段、preset 方案或厂商兼容分支
- `.gitignore`：config.json、`*config.json`、models/、tools/ffmpeg*.exe、cache/、logs/、.zcode/、.reasonix/、reasonix.toml
- 提交前自查：`git grep -nE "api_key|api_url|preset|sk-[A-Za-z0-9_-]{20,}"` 应无命中

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
