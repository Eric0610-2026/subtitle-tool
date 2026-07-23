#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置加载模块：读取 config.json 并转为 SimpleNamespace 对象
所有子模块统一通过 `from .config import cfg` 使用
"""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_CONFIG_PATH = Path(__file__).parent / "config.json"
_FALLBACK_PATH = Path(__file__).parent / "config.example.json"


def _dict_to_ns(d: Any) -> Any:
    if isinstance(d, dict):
        return SimpleNamespace(**{k: _dict_to_ns(v) for k, v in d.items()})
    if isinstance(d, list):
        return [_dict_to_ns(i) if isinstance(i, (dict, list)) else i for i in d]
    return d


class Config:
    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            path = _CONFIG_PATH if _CONFIG_PATH.exists() else _FALLBACK_PATH
        self._path = path
        self._data = self._load()

    def _load(self) -> SimpleNamespace:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise RuntimeError(f"配置文件不存在: {self._path}")
        except json.JSONDecodeError as e:
            raise RuntimeError(f"配置文件 JSON 格式错误 ({self._path}): {e}")
        except PermissionError as e:
            raise RuntimeError(f"配置文件无权限读取 ({self._path}): {e}")
        except OSError as e:
            raise RuntimeError(f"配置文件读取失败 ({self._path}): {e}")
        return _dict_to_ns(raw)

    def reload(self) -> None:
        self._data = self._load()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._data, name)

    def get_dict(self) -> dict:
        return json.loads(self._path.read_text(encoding="utf-8"))


cfg = Config()