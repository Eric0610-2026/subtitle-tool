# 更新日志

## 未发布 — 当前开发版（v1.2 → HEAD）

> 自 v1.2 (2026-07-31) 以来共 **18 个提交**，涉及 **31 个文件**，新增 **~4,334 行**，删除 **~1,415 行**。

---

### 新功能

- **本地翻译服务自动管理** — 新增 `local_service.py`，选择本地 Hy-MT2 模式时幂等自动拉起 `llama-server`（强制绑定 `127.0.0.1:8080`），应用退出时按端口清理残留进程。无需手动启动服务脚本。
- **GPU 两阶段调度** — 多文件并行流水线改为「阶段 1 全部转写 → 释放 Whisper → 阶段 2 全部翻译+嵌入」的两阶段调度，避免转写/翻译同时占显存导致 OOM。串行模式同样在转写后释放 Whisper 再进入翻译。
- **模型生命周期优化** — Whisper 启动不预加载，转写时才加载并常驻至切阶段；Hy-MT2 仅在翻译阶段启动；底部「🧠 当前加载」只显示实际已加载的模型。
- **系统通知** — 新增 `notifier.py`，处理完成时通过 winotify（首选）或 PowerShell BalloonTip（备选）发送 Windows 系统通知，含字符白名单防注入。
- **深色/浅色主题切换** — 新增 `theme.py` 模块，从 `config.json` 读取配色方案，窗口右上角太阳/月亮图标切换主题，支持自动检测系统主题。
- **字幕预览表格化** — 预览区从 HTML 表格改为 `QTableWidget` 控件，序号/时间轴/原文/译文四列并排，支持交替行背景、悬停/选中高亮、表格内直接编辑时间轴/原文/译文。
- **嵌入前暂停确认** — 翻译完成后弹出预览编辑对话框，用户可预览/编辑字幕后再执行嵌入操作，支持「嵌入」或「跳过」。
- **翻译缓存进程级共享** — 并行流水线中多个 `TranslationClient` 共享同一内存字典 + 全局锁，避免后写覆盖先写的缓存丢失。
- **批大小按翻译方式分离** — 本地模式默认 `batch_size=20`，联网模式默认 `batch_size_online=100`，手动修改可全局覆盖，永久保存时按模式写入对应字段。
- **ffmpeg 查找支持系统 PATH 回退** — 查找顺序：应用目录 → `tools/` → 系统 PATH，系统已装 ffmpeg 时无需再放置 `ffmpeg.exe`。
- **字幕质量检查** — `srt_utils.py` 新增 `analyze_subtitle_quality()`，自动检测空字幕、过短/过长、时间重叠、间隙过长等问题，输出结构化报告。
- **断点续转/续翻** — 转写每 30 段写入 `.partial.srt` 检查点，翻译写入 `.translate_state.json`，中断后重开可继续。
- **已有字幕翻译自动内嵌** — 找到同名视频且 ffmpeg 可用时自动内嵌生成 MKV，内嵌成功后删除原视频与翻译后的字幕（原字幕已备份至 `logs/srt_backup`）。

### 修复

- **修复字幕预览速度跟不上转写** — `PreviewPanel.append` 实时预览只保留最近 300 块，避免长视频时每次全量重建表格导致 UI 卡死。
- **修复翻译预览译文列重复显示原文** — 双语模式下 `final_texts` 已是「原文\n译文」合并文本，预览只取纯译文。
- **修复多视频转写+本地翻译场景的 8 个问题**：
  - 转写实时预览改为标准 SRT 块格式，修复右侧预览消失
  - 长句字符均分时每段至少 2 字符，杜绝单字字幕；`split_sentences` 补充日文断句标点
  - 翻译后预览改为标准块（序号/时间/原文/译文），修复译文挤一列
  - 翻译阶段开始后刷新「当前模型」标签，不再停留在 whisper
  - 本地模型心跳不再误报「API 响应较慢」
  - 递归拆批后子批 id 重新编号，返回条数不足时不再硬顺序回填，修复同一译文连续重复块
  - 模型把原文当译文返回时不写入缓存，留待补翻
  - Whisper 对循环语音的相邻重复块合并
- **修复线程池 daemon 化** — 翻译线程池改为 daemon 线程，停止后关闭窗口不再阻塞进程退出数分钟。
- **修复模型配置竞态** — 「获取列表/检测」增加请求序号，忽略旧请求的迟到回调。
- **修复 `is_bilingual` 缓存 key** — 双语/纯译文模式分开缓存，避免缓存混淆。
- **修复用户停止误报错误** — 用户停止时转写线程的「用户停止」异常不再上报为 error（串行+并行均修复）。
- **修复模型手动输入被列表覆盖** — `ModelConfigDialog.selected_model()` 优先返回手动输入框内容，列表选中项仅作回退。
- **修复联网模式批大小保存失效** — 新增 `_batch_size_save_field()`，按翻译方式写入对应字段，修复联网模式自定义值重启失效并污染本地默认值的问题。
- **修复 SRT 解析** — `parse` 保留空文本块（empty 统计生效）；块间缺空行时不再吞并下一块。
- **修复 `_sanitize_srt_for_mux` 误判** — 只比较公共前缀的误判修复，净化后块数减少（末尾空块被过滤）时不再跳过净化。
- **修复 curl fallback 文件名碰撞** — 临时文件加入线程标识，消除并发 403 时的文件名碰撞。
- **修复 ffmpeg 进度时间解析** — 按实际小数位数解析，修复精度问题。
- **修复空字符串 language** — 空字符串 `language` 走 auto 检测。
- **修复无 ffmpeg 时断点续转** — 回退避免 SRT 重复。
- **修复音频进度毫秒计算** — 修复音频处理进度显示不准确的 bug。
- **修复全缓存命中返回译文** — 当翻译全部命中缓存时，正确返回译文而非空结果。
- **修复深色模式勾选框与头部图标渲染** — 勾选框改用自绘白色勾选标记 PNG；头部主题按钮改用 `QPainter` 绘制太阳/月亮图标。
- **修复设置页方案增删按钮** — 改用 `QPainter` 绘制的圆形加号/减号图标，避免 emoji 渲染异常；API Key 输入框增加「显示/隐藏」切换按钮。
- **修复字幕输出路径** — 已有字幕翻译输出统一写在字幕所在目录，不再因同名视频进子目录或创建「视频名/」子目录。
- **修复模型加载进度条** — 模型加载不再体现在转写进度条中（仅日志提示）。
- **修复标签页扫描** — 切换标签页不再自动扫描；「📌 默认」按钮按当前标签页类型手动扫描。
- **修复翻译服务错误提示** — 移除对已删 `start-local-model.bat` 的引用，改为检查 `tools/llama-cpp` 与 `models/hy-mt2` 目录及 8080 端口占用。

### 架构 / 重构

- **模块拆分** — 从 `qt_app.py` 解耦出三个独立模块：
  - `theme.py`：主题配色加载、QSS 生成、图标绘制、系统深色模式检测
  - `notifier.py`：系统通知（winotify → PowerShell 备选）
  - `local_service.py`：本地翻译服务自动管理
- **Pipeline 重构** — `_run_staged()` 两阶段调度 + `_DaemonThreadPoolExecutor`（daemon 线程）+ `_prepare_transcribe_phase()` 清理逻辑。
- **信号桥重写** — `SignalBridge`（Qt Signal + 队列连接）替代 `queue.Queue` + `QTimer` 轮询，事件分发表预构建一次。
- **翻译客户端重构** — `_normalize_response()`、`_extract_error()`、`_apply_batch_translations()`、`_reassemble_blocks()` 独立函数化。
- **Muxer 重构** — `embed_subtitles_to_video()` 返回 `(mkv_path, is_trustworthy)` 二元组，`_verify_duration()` 时长验证独立。
- **SRT 工具函数增强** — `analyze_subtitle_quality()`、`OverallProgress` 类、`match_video_for_subtitle()`、`make_post_mapper()` 等。
- **繁简转换增强** — 扩充内置繁简转换表；opencc 优先，转换失败回退内置表；避免将日文特有汉字（駅、辻等）误判为中文。
- **事件分发表预构建** — `qt_app.py` 中 `_event_handlers` 字典只构建一次，避免每次事件查找。
- **备份保留上限** — `logs/srt_backup` 备份保留上限（`backup_max_files`，默认 50），超出自动清理最旧备份。
- **语言检测独立** — 默认每文件独立检测语言，`reuse_auto_lang` 可开启同批复用。
- **删除无用脚本** — 移除 `tools/clean_srt_empties.py`（功能已整合进 `srt_utils.py`）。

### UI / UX

- 深色/浅色主题切换（窗口右上角切换按钮）
- 田字型四象限布局（文件列表 + 预览 + 进度 + 日志）
- 字幕预览表格化（支持表格内编辑）
- 总进度条 + 跨文件 ETA 估算
- 转写/翻译双进度条分离
- 系统通知（处理完成时弹出）
- 窗口状态恢复/保存
- 已有字幕翻译页自动扫描默认目录
- 下拉框双箭头修复（自绘箭头 PNG，深浅两套）
- 复选框文字黑底问题修复，识别选项间距调整
- 预览区文字居中、空状态提示位置、底部灰框修正
- 模型配置页「显示/隐藏」按钮固定宽度文字截断修复
- 日志区压缩，翻译批次提交日志合并为一条
- 去除系统弹窗声音
- 统一标题样式

### 配置变更

| 新增字段 | 类型 | 默认值 | 说明 |
|----------|------|--------|------|
| `whisper.reuse_auto_lang` | bool | `false` | auto 模式下复用同批首个检测语言 |
| `translation.batch_size` | int | `20` | 本地模式批大小（默认值从 50 改为 20） |
| `translation.batch_size_online` | int | `100` | 联网模式批大小 |
| `translation.mode` | string | `"local"` | 默认翻译模式 |
| `translation.backup_max_files` | int | `50` | SRT 备份保留份数 |
| `theme.light/dark` | object | — | 浅色/深色主题配色（bg, card, header, accent, text 等） |
| `app.notification_duration_ms` | int | `8000` | 系统通知时长 |
| `app.notification_sleep_s` | int | `9` | 通知间隔秒数 |
| `app.scan_skip_exts` | array | `[".mkv"]` | 扫描跳过的扩展名 |
| `.gitignore` | — | — | 新增 `models/`、`tools/llama-cpp/` 忽略规则 |

### 测试

- **新增 `test_local_service.py`** — 197 行，覆盖本地服务启动/探测/清理/心跳逻辑
- **全部测试文件大幅扩写** — 从 7 个文件 129 例 → 9 个文件 ~192 例
- 新增 `tools/tests/__init__.py`，统一测试进程 stdout/stderr 编码为 UTF-8，修复 Windows GBK 乱码
- 新增停止场景回归测试（串行 + 并行）
- 新增批大小持久化字段测试（`TestBatchSizePersistenceField`，四路字段选择）
- 新增翻译缓存 key 隔离测试（双语/纯译文模式）
- 新增预览性能测试（300 块截断）
- 全量测试覆盖通过

### 文档

- **新增 `docs/使用说明书.md`** — 1,355 行完整用户手册，含零基础入门篇、安装指南、界面详解、操作流程、配置参考、故障排除等
- **AGENTS.md 移至根目录** — 从 `docs/AGENTS.md` 移至根目录，使其自动进入 AI 新会话上下文；同步更新架构说明（173 例测试、SignalBridge、GPU 两阶段调度、补齐 local_service/notifier/theme 模块）
- **README.md 更新** — 功能亮点列表、安装/使用说明、ffmpeg 查找顺序说明、常见问题、协议声明
- **删除 `docs/README.md`** — 无用占位文档
- **删除 `docs/AGENTS.md`** — 已迁移至根目录

### 杂项

- **删除 CHANGELOG.md** — 改用此文件替代
- **移除 GitHub Actions CI workflow** — CI 环境与本机环境存在差异，不再自动触发测试
- **清理无用文件** — 占位文档、命令行清理脚本、gitignore 规则优化