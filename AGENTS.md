# Repository Guidelines
 
 ## Project Structure & Module Organization
 
 The project is organized as a single Python package under `zimu_app/` with 11 source modules, each owning a distinct responsibility:
 
 | Module | Role |
 |---|---|
 | `qt_app.py` | Qt (PySide6) main window UI, event dispatch, theme, preview |
 | `widgets.py` | Custom Qt widgets: `DropListWidget` and `LogEntry` |
 | `transcriber.py` | Audio extraction + faster-whisper transcription |
 | `translation.py` | AI translation client, caching, batch, curl fallback |
 | `translator.py` | Translation pipeline orchestration (consume, translate, assemble) |
 | `pipeline.py` | Serial/parallel pipeline orchestration, subprocess management, stop |
 | `srt_utils.py` | SRT parse/write, sentence splitting, trad/simp conversion, progress tracking |
 | `muxer.py` | MKV soft-sub embedding, .ts repair, duration validation |
 | `dialogs.py` | Settings, history, and cache management dialogs |
 | `config.py` | Loads `config.json` into a `SimpleNamespace` singleton (`cfg`) |
 
 - Entry point: `subtitle_app.py` -> `zimu_app.qt_app.main()`
 - All configuration lives in `zimu_app/config.json`; create it from `config.example.json`
 - Third-party binaries (`ffmpeg.exe`, `ffprobe.exe`) and the Whisper model directory (`faster-whisper-large-v3-turbo/`) sit at the repo root
 - Runtime cache and state files (`.subtitle_*.json`) are git-ignored and auto-generated
 
 ## Build, Test, and Development Commands
 
 All commands run from the repository root:
 
 - **Run the application:** `python subtitle_app.py`, or double-click `启动字幕工具.bat` (launches with `pythonw.exe`, no console)
 - **Run all tests:** `python -m unittest discover -s tests`
 - **Run a single test file:** `python -m unittest tests.test_srt_utils`
 - **Run a single test case:** `python -m unittest tests.test_srt_utils.TestSrtRoundtrip`
 
 No build step is needed. Dependencies (PySide6, faster-whisper, etc.) are installed via `pip install -r requirements.txt`.
 
 ## Coding Style & Naming Conventions
 
 - **Python version:** 3.10+ (uses `Path | None` union syntax)
 - **Indentation:** 4 spaces, no tabs
 - **Strings:** double quotes by convention
 - **Naming:** `snake_case` for functions and variables, `PascalCase` for classes, `UPPER_CASE` for module-level constants, `_` prefix for private helpers
 - **Type hints:** required for function signatures; prefer standard library generics (`list[str]` over `List[str]`)
 - **Imports:** stdlib -> third-party -> local, each group separated by a blank line
 - **Headers:** every `.py` file uses `#!/usr/bin/env python3` + `# -*- coding: utf-8 -*-`
 - **Logging:** always use `logger = logging.getLogger(__name__)` per module
 - **Linting/formatting:** none configured -- consistency is maintained by convention
 
 ## Testing Guidelines
 
 - **Framework:** standard library `unittest` only -- no pytest, no plugins
 - **Coverage:** no coverage tool configured; aim to cover core parsing, I/O, and edge cases
 - **Test file placement:** `tests/test_<module_name>.py`, import from `zimu_app.<module>`
 - **Test class naming:** `Test<Thing>` or `Test<Verb><Thing>`, extending `unittest.TestCase`
 - **Test method naming:** `test_<what_it_tests>` in snake_case
 - **Fixtures:** use `setUp()` for shared state; use `tempfile.TemporaryDirectory` for file I/O tests
 - **Mocking:** `unittest.mock` for network and API calls in `test_translation.py`
 
 Currently 48 tests across 2 files (22 in `test_srt_utils.py`, 26 in `test_translation.py`). Keep tests fast and focused -- no external network or model loading.
 
 ## Commit & Pull Request Guidelines
 
 This repo uses one commit per meaningful change with descriptive messages in Chinese:
 
 ```
 <brief description>: <specific feature>
 ```
 
 Example: `初始提交：字幕生成与双语翻译工具`
 
 Guidelines:
 
 - Write commit messages in Chinese, describing what and why, not how
 - Keep commits atomic -- one logical change per commit
 - Verify all 48 tests pass before pushing
 - Do not commit API keys: `config.json` is git-ignored; always commit changes to `config.example.json` instead
 - PR descriptions should summarize the change, mention affected modules, and include UI screenshots for visual changes
 
 ## Configuration & Security Notes
 
 - **`config.json` contains plaintext API keys** -- it is listed in `.gitignore` and must never be committed. Use `config.example.json` as the template.
 - After changing `config.json`, **restart the application** -- each module reads config at import time and caches it as a module-level `cfg` constant; there is no hot-reload.
 - The `.gitignore` also excludes runtime cache files (`.subtitle_*.json`), model binaries (`faster-whisper-large-v3-turbo/`, `ffmpeg.exe`, `ffprobe.exe`), and IDE/OS metadata.
 
 ## Architecture Overview
 
 Media file -> Transcriber -> `{video}.{lang}.srt` -> `translator.py` -> `muxer.py` (MKV embed) or external SRT
 
 - **Thread model:** Qt main thread (UI) + worker thread (transcription) + queue (translation consumption), communicating via a bounded `ui_queue` (maxsize=2000)
 - **Concurrency:** serial (`concurrency=1`) or parallel pipeline (>=2 runs transcription N+1 in parallel with translation N)
 - **Checkpointing:** both transcription and translation write partial state files (`.partial.srt`, `*.translate_state.json`), enabling resume after crash
 - **Data sanitization:** timestamps are validated at two stages -- before SRT write in `transcriber.py` (`sanitize_blocks`) and before MKV muxing (`_sanitize_srt_for_mux`)
 
 ## Agent-Specific Instructions
 
 When working with this codebase as an AI agent:
 
 - Use `rg` or `Select-String` for fast code search instead of `grep`
 - Read existing module patterns before introducing new abstractions -- the project values simplicity and explicit code over indirection
 - Respect the module boundaries in the table above; each file has a clear, single responsibility
 - After changes, run all 48 tests to confirm nothing is broken
 - Never commit `config.json` or any file containing plaintext API keys
