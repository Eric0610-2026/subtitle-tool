# Repository Guidelines

## Project Structure & Module Organization

- `electron/` contains the desktop interface and Windows integration. `subtitle_app/electron_backend.py` connects it to the Python processing modules.
- `pipeline.py` coordinates transcription (`transcriber.py`), translation (`translation.py`, `translator.py`), and media processing (`muxer.py`). `local_service.py` manages local inference.
- `tools/tests/` contains unittest suites. `tools/requirements.txt` lists Python backend dependencies. `subtitle_app/icon.ico` is the application icon.
- `models/`, `cache/`, and `logs/` hold local runtime data. See `README.md` and `docs/` for installation and maintenance instructions.

## Build, Test, and Development Commands

Run from the repository root in Windows PowerShell:

```powershell
python -m pip install -r tools/requirements.txt
npm ci
npx install-electron
npm start
python -m unittest discover -s tools/tests
python -m unittest tools.tests.test_translator
git diff --check
```

The root Windows launcher starts the installed Electron executable. Rebuild it with `tools/build-launcher.ps1` when the launcher source changes. Actual media processing requires the tools and models described in README.

## Coding Style & Naming Conventions

Use four-space indentation, predominantly double-quoted strings, and existing type-annotation conventions. Use `snake_case` for functions/modules and `PascalCase` for classes. Follow surrounding imports and use `logging.getLogger(__name__)`. Keep module responsibilities focused; avoid unnecessary abstractions. No repository-wide formatter or linter configuration is prescribed.

## Testing Guidelines

Use standard-library `unittest`, `test_*.py` files, and `test_*` methods. Add regression coverage for behavioral fixes and run the full suite after every change. No numerical coverage threshold is defined.

Mock network requests, model loading, service startup, and media subprocesses. Isolate configuration, caches, and outputs in temporary directories; patch `translator._BACKUP_DIR` for translation orchestration tests. Never use production backups or user media. Inspect rendered output for UI changes.

## Commit & Pull Request Guidelines

History favors short imperative subjects such as “Fix subtitle parsing” or “Improve subtitle processing”; `docs:` also appears. Keep commits focused. PRs should explain the problem, resulting behavior, validation, and limitations. Link relevant issues and include screenshots for visible UI changes.

## Configuration & Collaboration

Keep personal `config.json`, models, caches, and logs out of commits. Maintain defaults in `subtitle_app/config.example.json`. Translation must remain local through `local_service.translation_endpoint()`; do not introduce external endpoints or credentials.

Confirm new features and unclear requirements with the user. Preserve existing changes, synchronize affected documentation, and recommend a next step after modifications. Do not commit, push, or publish without authorization.
