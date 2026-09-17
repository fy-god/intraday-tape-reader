"""配置加载：settings.yaml / watchlist.yaml / holidays.txt 的唯一入口。

任何模块都不许自己 yaml.safe_load，一律用本模块（见 docs/DATA_CONTRACT.md 第 8 节）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "PROJECT_ROOT",
    "Settings",
    "load_settings",
    "load_watchlist",
    "load_holidays",
    "source_cfg",
    "rule_cfg",
    "reload_watchlist",
]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS = PROJECT_ROOT / "config" / "settings.yaml"
DEFAULT_WATCHLIST = PROJECT_ROOT / "config" / "watchlist.yaml"
DEFAULT_HOLIDAYS = PROJECT_ROOT / "config" / "holidays.txt"


# --------------------------------------------------------------------------
# 默认值（settings.yaml 缺字段时兜底，保证旧配置仍能跑）
# --------------------------------------------------------------------------
DEFAULTS: dict[str, Any] = {
    "app": {
        "name": "A股盘中雷达",
        "timezone": "Asia/Shanghai",
        "log_level": "INFO",
        "data_dir": "data",
        "dry_run": True,
        "replay": False,
    },
    "poll": {
        "universe_seconds": 5,
        "watchlist_seconds": 3,
        "universe_refresh_seconds": 1800,
        "history_len": 360,
        "batch_size": 600,
        "workers": 4,
        "http_timeout": 10,
        "retries": 3,
        "idle_when_closed": True,
    },
    "session": {
        "warmup_seconds": 60,
        "record_auction": True,
        "holidays_file": "config/holidays.txt",
    },
    "sources": {
        "primary": "tencent",
        "universe": "eastmoney",
        "fallback": ["sina"],
        "failover_threshold": 3,
    },
    "filters": {
        "min_price": 1.5,
        "max_price": 2000.0,
        "min_amount": 8_000_000,
        "exclude_st": False,
        "exclude_boards": ["index"],
        "exclude_codes": [],
        "min_list_days": 11,
    },
    "rules": {},
    "notify": {"enabled": ["console"]},
    "storage": {
        "alerts_path": "data/alerts.jsonl",
        "snapshot_every": 0,
        "snapshot_path": "data/snapshots.jsonl",
        "series_len": 240,
    },
    "web": {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 8899,
        "sse_interval": 2,
        "top_n": 30,
        "max_alerts": 300,
        "title": "A股盘中雷达 · 急拉急跌预警",
    },
}


def _deep_merge(base: dict, over: dict) -> dict:
    """把 over 合并进 base 的副本（dict 递归，其余覆盖）。"""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass(slots=True)
class Settings:
    raw: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None

    # --- 便捷访问 -------------------------------------------------------
    def section(self, name: str) -> dict[str, Any]:
        v = self.raw.get(name)
        return v if isinstance(v, dict) else {}

    def get(self, dotted: str, default: Any = None) -> Any:
        """settings.get("poll.universe_seconds") """
        cur: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    # --- 常用项（带默认值，避免调用方到处写 default） -------------------
    @property
    def dry_run(self) -> bool:
        return bool(self.get("app.dry_run", True))

    @property
    def replay(self) -> bool:
        return bool(self.get("app.replay", False))

    @property
    def data_dir(self) -> Path:
        d = Path(str(self.get("app.data_dir", "data")))
        return d if d.is_absolute() else PROJECT_ROOT / d

    @property
    def poll(self) -> dict[str, Any]:
        return self.section("poll")

    @property
    def filters(self) -> dict[str, Any]:
        return self.section("filters")

    @property
    def web(self) -> dict[str, Any]:
        return self.section("web")

    @property
    def storage(self) -> dict[str, Any]:
        return self.section("storage")

    def rule(self, name: str) -> dict[str, Any]:
        return rule_cfg(self, name)

    def resolve(self, p: str | os.PathLike) -> Path:
        """相对路径按项目根解析。"""
        path = Path(p)
        return path if path.is_absolute() else PROJECT_ROOT / path


_CACHE: dict[str, Any] = {}


def load_settings(path: str | os.PathLike | None = None, *, use_cache: bool = True) -> Settings:
    """读取 settings.yaml，缺失字段用 DEFAULTS 补齐。

    ``use_cache=False`` 表示"要一份**独占**的副本"——既不读缓存，**也不写缓存**。
    只跳过读、仍然回写的话，调用方改一改这份"私有"配置就会污染进程内的共享
    实例（实测：一个测试改了 rules.*.enabled，导致同进程的回放测试全部 0 告警）。

    **``path`` 是覆盖层，不是替换**（自定义配置会**叠加在**默认 settings.yaml 之上）：

        默认 settings.yaml  <-  DEFAULTS（代码兜底）  <-  path（本次指定的文件）

    为什么是叠加而不是替换：``path`` 的典型用途是"我只想改两三个开关"
    （比如开盘时打开短线精灵），此时**没人会想把 ``poll.index_codes``、
    ``sources.universe`` 这些没提到的设置一起清空**。早先的实现是直接替换，
    于是 ``--config config/settings.live.yaml`` 会静默丢掉 ``index_codes``
    （该键只在 YAML 里、不在 DEFAULTS 里），表现为"spirit_index 明明 enabled
    却永远不出信号"——一个很难查的静默失效。

    想**完全替换**（不要默认值）就传一个显式的完整配置：叠加语义下，
    后写的键值总是赢，所以完整配置的行为与替换一致。
    """
    p = Path(path) if path else DEFAULT_SETTINGS
    key = str(p)
    if use_cache and key in _CACHE:
        return _CACHE[key]

    def _read(fp: Path) -> dict[str, Any]:
        if not fp.exists():
            return {}
        with open(fp, "r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        return loaded if isinstance(loaded, dict) else {}

    # 默认文件先铺底，再叠 DEFAULTS，最后叠调用方指定的覆盖层。
    # 顺序要紧：覆盖层必须最后应用，否则它会被默认文件里的同名键盖掉。
    data = _read(DEFAULT_SETTINGS)
    if p != DEFAULT_SETTINGS:
        data = _deep_merge(data, _read(p))
    merged = _deep_merge(DEFAULTS, data)
    st = Settings(raw=merged, path=p)
    if use_cache:
        _CACHE[key] = st
    return st


def clear_cache() -> None:
    _CACHE.clear()


def _norm_code(item: Any) -> str | None:
    """'600000' / 'sh600000' / 'SH600000' / 600000 -> '600000'，非法返回 None。

    注意：YAML 1.1 会把裸写的 ``000001`` 解析成八进制整数 1，
    因此数字类型必须先零填充到 6 位，不能直接 str()。
    """
    if item is None or isinstance(item, bool):
        return None
    if isinstance(item, int):
        if 0 <= item <= 999999:
            return f"{item:06d}"
        return None
    if isinstance(item, float):
        if item.is_integer() and 0 <= item <= 999999:
            return f"{int(item):06d}"
        return None
    s = str(item).strip().upper()
    if not s:
        return None
    if s[:2] in ("SH", "SZ", "BJ"):
        s = s[2:]
    s = s.strip()
    if len(s) == 6 and s.isdigit():
        return s
    # 容忍 '600000.SH' / '600000.SZ'
    if len(s) > 6 and s[:6].isdigit():
        return s[:6]
    return None


def load_watchlist(path: str | os.PathLike | None = None) -> list[str]:
    """读取自选股，返回去重后的 6 位代码列表（保持文件顺序）。"""
    return _load_codes(path, DEFAULT_WATCHLIST, "watchlist")


def load_ignore(path: str | os.PathLike | None = None) -> list[str]:
    return _load_codes(path, DEFAULT_WATCHLIST, "ignore")


def load_focus(path: str | os.PathLike | None = None) -> list[str]:
    return _load_codes(path, DEFAULT_WATCHLIST, "focus")


def _load_codes(path: str | os.PathLike | None, default: Path, key: str) -> list[str]:
    p = Path(path) if path else default
    if not p.exists():
        return []
    with open(p, "r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh) or {}
    if not isinstance(loaded, dict):
        return []
    items = loaded.get(key) or []
    if isinstance(items, str):
        items = [items]
    out: list[str] = []
    seen: set[str] = set()
    for it in items:
        c = _norm_code(it)
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def reload_watchlist(path: str | os.PathLike | None = None) -> list[str]:
    """前端/CLI 热加载用（当前无缓存，保持接口稳定）。"""
    return load_watchlist(path)


def load_holidays(path: str | os.PathLike | None = None) -> set[str]:
    """读取休市日，返回 {'2026-01-01', ...}。"""
    p = Path(path) if path else DEFAULT_HOLIDAYS
    if not p.exists():
        return set()
    days: set[str] = set()
    with open(p, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            line = line.replace("/", "-").replace(".", "-")
            parts = line.split("-")
            if len(parts) == 3:
                try:
                    days.add(f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}")
                except ValueError:
                    continue
    return days


def source_cfg(name: str, settings: Settings | None = None) -> dict[str, Any]:
    """取某数据源的配置节（含通用 poll 参数）。"""
    st = settings or load_settings()
    cfg = dict(st.section("sources").get(name) or {})
    poll = st.poll
    cfg.setdefault("timeout", poll.get("http_timeout", 10))
    cfg.setdefault("retries", poll.get("retries", 3))
    cfg.setdefault("batch_size", poll.get("batch_size", 600))
    cfg.setdefault("workers", poll.get("workers", 4))
    return cfg


def rule_cfg(settings_or_name, name: str | None = None) -> dict[str, Any]:
    """rule_cfg(settings, "tick_surge") 或 rule_cfg("tick_surge")。"""
    if name is None:
        st = load_settings()
        name = str(settings_or_name)
    else:
        st = settings_or_name
    cfg = dict(st.section("rules").get(name) or {})
    cfg.setdefault("enabled", True)
    return cfg
