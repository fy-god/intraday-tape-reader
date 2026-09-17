"""配置接线：三个短线精灵模块的 DEFAULTS、settings.yaml 与引擎装配必须一致。

这一组测试的存在理由是一次真实踩坑：模块里写 ``DEFAULTS["enabled"] = False``
**拦不住** ``config.rule_cfg()`` —— 它对缺失的配置节会 ``setdefault("enabled", True)``
并经 ``ctx.cfg`` 下发，于是"默认关闭"的模块照样会被启用。所以"YAML 里有这一节
且 enabled: false"是**功能的一部分**，不是可有可无的文档，必须测住。

同理，``DEFAULTS`` 里有的键 YAML 里没有时，运维改了配置会以为生效了其实没有；
``max_per_round: 0``（约定 = 不限量）被 ``or N`` 惯用法换成默认值也是同一类
"静默不生效"缺陷，两个模块都真实发生过。
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arad.config import load_settings          # noqa: E402
from arad.engine import RULE_MODULES, build_rules  # noqa: E402

SPIRIT = ("spirit_price", "spirit_order", "spirit_index")


@pytest.fixture(scope="module")
def settings():
    return load_settings()


@pytest.mark.parametrize("name", SPIRIT)
def test_yaml_section_exists_and_is_disabled_by_default(name, settings):
    """缺这一节 => rule_cfg 会 setdefault(True) => 模块被意外启用。"""
    sec = settings.section("rules").get(name)
    assert sec is not None, (
        f"config/settings.yaml 缺少 rules.{name} 节；"
        f"缺了它 rule_cfg() 会把 enabled 默认成 True"
    )
    assert sec.get("enabled") is False, (
        f"rules.{name}.enabled 应为 false（默认关闭，避免与 tick_surge 重复刷屏）"
    )


def test_spirit_modules_absent_from_default_rule_set(settings):
    """默认配置下装配出的规则里不该出现任何 spirit 模块。"""
    names = {getattr(r, "name", type(r).__name__) for r in build_rules(settings)}
    for name in SPIRIT:
        assert name not in names, f"{name} 被默认启用了"
    assert names <= set(RULE_MODULES)


@pytest.mark.parametrize("name", SPIRIT)
def test_every_default_key_is_present_in_yaml(name, settings):
    """DEFAULTS 的每个键都要能在 YAML 里找到，否则改配置静默不生效。"""
    mod = importlib.import_module(f"arad.rules.{name}")
    defaults = set(getattr(mod, "DEFAULTS", {}) or {})
    sec = settings.section("rules").get(name) or {}
    missing = sorted(defaults - set(sec))
    assert not missing, f"settings.yaml 的 rules.{name} 缺少 {missing}"


@pytest.mark.parametrize("name", SPIRIT)
def test_max_per_round_zero_means_unlimited(name):
    """显式 ``max_per_round: 0`` 必须保持 0（不限量），不能被 `or N` 改写。"""
    mod = importlib.import_module(f"arad.rules.{name}")
    cfg = dict(mod.DEFAULTS)
    cfg["enabled"] = True
    cfg["max_per_round"] = 0
    rule = mod.build(cfg)

    stored = getattr(rule, "max_per_round", None)
    if stored is not None:
        assert stored == 0, f"{name}: 显式 0 被改成 {stored!r}"
        return

    # 每轮从 ctx.cfg 现取的模块（spirit_order）：直接问它的取值函数
    getter = getattr(rule, "_get", None)
    assert callable(getter), f"{name}: 既无 max_per_round 属性也无 _get，无法核对"

    class _Ctx:
        cfg = {"max_per_round": 0}

    assert getter(_Ctx(), "max_per_round", 20) == 0, f"{name}: 显式 0 没被原样取出"


@pytest.mark.parametrize("name", SPIRIT)
def test_build_honours_enabled_false(name):
    """``build({"enabled": False})`` 出来的规则必须自认关闭。"""
    mod = importlib.import_module(f"arad.rules.{name}")
    cfg = dict(mod.DEFAULTS)
    cfg["enabled"] = False
    rule = mod.build(cfg)

    class _Snap:
        ts = None
        seq = 0
        quotes: dict = {}

    class _Ctx:
        now = None
        cfg: dict = {}

        def __getattr__(self, item):        # 规则可能读 session/state 等
            raise AttributeError(item)

    # enabled=False 时应尽早返回空，不碰 ctx 的其它属性
    assert rule.evaluate(_Snap(), _Ctx()) == []


@pytest.mark.parametrize("name", SPIRIT)
def test_module_declares_signal_names_for_presentation(name):
    """每个模块都要声明 SIGNALS，展示层靠它把 pattern 翻成中文。"""
    mod = importlib.import_module(f"arad.rules.{name}")
    signals = getattr(mod, "SIGNALS", None)
    assert signals, f"{name} 没有 SIGNALS"

    from arad.spirit import SIGNALS as CATALOGUE

    for sig in signals:
        assert sig in CATALOGUE, (
            f"{name} 的信号 {sig!r} 展示层不认识，会静默降级成兜底名"
        )


# ==========================================================================
# load_settings 的缓存语义
# ==========================================================================
def test_use_cache_false_returns_isolated_copy_and_does_not_poison_cache():
    """``use_cache=False`` 必须"既不读缓存、也不写缓存"。

    只跳过读、仍然回写的话，调用方一改这份"私有"配置就污染了进程内的共享实例。
    这是一个真实缺陷：一个测试改了 ``rules.*.enabled`` 之后，同进程的整组回放
    测试全部变成 0 条告警，而单跑那个文件却完全正常 —— 极难定位。
    """
    from arad.config import clear_cache, load_settings

    clear_cache()
    try:
        # 先建立缓存
        cached = load_settings()
        before = cached.section("rules")["tick_surge"]["enabled"]

        # 拿一份独占副本并改坏它
        private = load_settings(use_cache=False)
        private.section("rules")["tick_surge"]["enabled"] = not before

        # 缓存里的那份必须纹丝不动
        assert load_settings().section("rules")["tick_surge"]["enabled"] is before, \
            "use_cache=False 仍然回写了缓存，私有副本污染了共享实例"
        # 两次私有副本互不影响
        assert load_settings(use_cache=False) is not private
        assert private is not cached
    finally:
        clear_cache()


def test_cached_load_returns_same_object():
    """默认（带缓存）确实复用同一个对象 —— 上面那条测试的前提。"""
    from arad.config import clear_cache, load_settings

    clear_cache()
    try:
        assert load_settings() is load_settings()
    finally:
        clear_cache()


# ==========================================================================
# path 参数是"覆盖层"，不是"替换"
# ==========================================================================
def test_custom_config_is_an_overlay_not_a_replacement(tmp_path):
    """传入自定义配置时，**没提到的键必须继承默认 settings.yaml**。

    这曾经是个静默失效：``--config`` 直接**替换**了整个配置，于是
    ``config/settings.live.yaml``（只想打开几个开关）会把
    ``poll.index_codes``、``sources.universe`` 一起清空 —— 而这两个键只在
    YAML 里、不在代码 DEFAULTS 里，补不回来。

    症状是"``spirit_index`` 明明 enabled 却永远不出信号"，
    因为指数代码列表变成了空。这种"开关是对的、数据是空的"最难查，
    所以用测试钉住：覆盖层生效 + 未提及的键继承。
    """
    from arad.config import clear_cache, load_settings

    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        "rules:\n"
        "  spirit_price:\n"
        "    enabled: true\n"
        "app:\n"
        "  dry_run: false\n",
        encoding="utf-8")

    clear_cache()
    try:
        base = load_settings(use_cache=False)
        st = load_settings(overlay, use_cache=False)

        # 1) 覆盖层里的键生效
        assert st.get("rules.spirit_price.enabled") is True
        assert st.get("app.dry_run") is False

        # 2) 覆盖层**没提到**的键必须继承默认文件（而不是变 None）
        assert st.get("poll.index_codes") == base.get("poll.index_codes"), \
            "自定义配置把 poll.index_codes 清空了 —— 覆盖层退化成了替换"
        assert st.get("poll.index_codes"), "index_codes 不能为空，否则 spirit_index 静默失效"
        assert st.get("sources.universe") == base.get("sources.universe")
        assert st.get("poll.universe_seconds") == base.get("poll.universe_seconds")

        # 3) 覆盖层不该反向污染默认配置
        assert load_settings(use_cache=False).get("rules.spirit_price.enabled") is False, \
            "加载覆盖层污染了默认配置"
    finally:
        clear_cache()


def test_overlay_does_not_leak_into_default_settings():
    """加载覆盖层之后，默认配置必须还是原来那份（开关没被带偏）。"""
    from arad.config import clear_cache, load_settings

    clear_cache()
    try:
        before = load_settings(use_cache=False).get("rules.tick_surge.enabled")
        load_settings("config/settings.live.yaml", use_cache=False)
        after = load_settings(use_cache=False).get("rules.tick_surge.enabled")
        assert before == after, "加载 live 覆盖层后默认配置被改动了"
    finally:
        clear_cache()


# ==========================================================================
# 配置键必须真的被消费（"声明了但没人读"是静默失效）
# ==========================================================================
def test_storage_series_len_actually_reaches_the_store():
    """``storage.series_len`` 必须真的传到 AlertStore。

    这是一个真实的静默失效：``AlertStore.__init__`` 的 ``series_len`` 有
    硬编码默认值 240，它**不会**自己去看 settings。而 engine 里原本写的是
    ``AlertStore(self.settings, calendar=...)`` —— 少传了这一个参数，
    于是 YAML 里改 ``storage.series_len`` 完全没有效果，也不报错。

    为什么危险：这个键是**内存开关**。全市场 5563 只都记分时约 412 MB，
    调小它是降内存最直接的手段；一个"改了没反应"的键会让人以为已经生效，
    然后在长跑里被内存问题咬到（实测一天 2880 轮，内存会爬到稳定值）。

    ``check_orphan_config.py`` 抓不到它：那个检查只确认键名作为字符串
    字面量存在过，而 ``"series_len"`` 确实出现在 store.py 的 DEFAULTS 里。
    "名字存在"不等于"接线正确"。
    """
    from arad.config import clear_cache, load_settings
    from arad.engine import Engine

    clear_cache()
    try:
        st = load_settings(use_cache=False)
        st.raw.setdefault("storage", {})["series_len"] = 7
        eng = Engine(None, settings=st)
        assert eng.store._series_len == 7, (
            f"storage.series_len=7 没传到 AlertStore，"
            f"实际是 {eng.store._series_len}（配置被静默忽略）")

        # 默认值也要对
        st2 = load_settings(use_cache=False)
        assert Engine(None, settings=st2).store._series_len == 240
    finally:
        clear_cache()
