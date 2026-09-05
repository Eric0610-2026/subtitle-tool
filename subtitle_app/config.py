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


def _deep_merge(defaults: dict, override: dict) -> dict:
    """以 defaults 为基底、override 递归覆盖，返回合并后的新 dict。

    老用户的 config.json 缺少后期新增字段（如 notification_duration_ms），
    缺字段处用 example 的默认值补齐，避免消费方抛 AttributeError。
    """
    merged = dict(defaults)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = _deep_merge(merged[k], v)
        else:
            merged[k] = v
    return merged


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
        if (self._path != _FALLBACK_PATH and _FALLBACK_PATH.exists()
                and isinstance(raw, dict)):
            try:
                defaults = json.loads(_FALLBACK_PATH.read_text(encoding="utf-8"))
                if isinstance(defaults, dict):
                    raw = _deep_merge(defaults, raw)
            except (OSError, json.JSONDecodeError):
                pass  # example 自身读不了就按原配置加载，不因补默认值而阻断启动
        return _dict_to_ns(raw)

    def reload(self) -> None:
        """重新读取配置文件。

        范围有限：只影响运行期通过 `cfg.xxx` 动态读取的配置
        （如 batch_size、concurrency_translate、backup_max_files）。
        各模块 import 时固化的模块级常量（translation.API_TIMEOUT、
        srt_utils.VIDEO_EXTS、transcriber._MODEL_SPEED、theme 配色等）
        不会刷新——这些字段完整生效需重启应用。
        """
        self._data = self._load()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._data, name)

    def get_dict(self) -> dict:
        return json.loads(self._path.read_text(encoding="utf-8"))


cfg = Config()