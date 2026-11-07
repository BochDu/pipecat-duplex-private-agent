"""配置加载：config/defaults.env + .env + config/providers.toml。

设计约束：

- 调好的默认值在 `config/defaults.env`，随代码版本化，用户不用改。
- `.env` 只放凭据与机器差异（设备名等），每台机器一份、不入库。
- 覆盖顺序：`config/defaults.env` < `.env` < 真实环境变量。
- 配置组按注册表声明的 `config_group` 前缀切分，组内键去掉前缀后交给 Provider。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # Python >= 3.11 自带
    import tomllib
except ModuleNotFoundError:  # < 3.11 走 tomli（requirements 已按版本声明）
    import tomli as tomllib  # type: ignore[no-redef]

ROOT = Path(__file__).resolve().parent.parent

VALID_LINKS = {"s2s", "llm"}

#: 仓库内的默认值文件，相对仓库根
DEFAULTS_ENV = Path("config") / "defaults.env"


def _parse_env(text: str) -> dict[str, str]:
    """解析 KEY=VALUE 行；注释独占一行（与 preflight 脚本规则一致）。"""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _split_by_group(
    values: dict[str, str], groups: list[str]
) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """按最长前缀把键归入配置组，未命中的进 common。"""
    common: dict[str, str] = {}
    by_group: dict[str, dict[str, str]] = {}
    for key, value in values.items():
        matched = max(
            (group for group in groups if key.startswith(group + "_")),
            key=len,
            default=None,
        )
        if matched is None:
            common[key] = value
        else:
            by_group.setdefault(matched, {})[key[len(matched) + 1 :]] = value
    return common, by_group


@dataclass(frozen=True)
class WakewordEntry:
    """注册表里的一条：唤醒词 → 配置组 → 链路 → Provider。

    `aliases` 是该唤醒词的其它写法（同音、误识别），任一命中都算唤醒，
    返回给路由的始终是 `word`。
    """

    word: str
    config_group: str
    link: str
    provider: str
    aliases: tuple[str, ...] = ()


@dataclass
class Settings:
    """私有值视图：共用项在 common，品牌项按配置组切好。"""

    root: Path
    common: dict[str, str] = field(default_factory=dict)
    groups: dict[str, dict[str, str]] = field(default_factory=dict)

    @classmethod
    def load(cls, root: Path = ROOT, groups: list[str] | None = None) -> "Settings":
        """仓库默认值 → `.env` → 真实环境变量，逐层覆盖。"""
        values: dict[str, str] = {}
        for path in (root / DEFAULTS_ENV, root / ".env"):
            if path.exists():
                values.update(_parse_env(path.read_text(encoding="utf-8")))
        # 真实环境变量覆盖文件里已声明的键，便于部署与测试。
        for key in list(values):
            if key in os.environ:
                values[key] = os.environ[key]
        common, by_group = _split_by_group(values, groups or [])
        return cls(root=root, common=common, groups=by_group)

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.common.get(key, default)

    def group(self, name: str) -> dict[str, str]:
        return dict(self.groups.get(name, {}))

    def has_group(self, name: str) -> bool:
        return bool(self.groups.get(name))

    def num(
        self,
        key: str,
        default: float,
        *,
        lo: float | None = None,
        hi: float | None = None,
    ) -> float:
        """读浮点值：缺失或写错都回落默认值，再按 `lo` / `hi` 夹紧。

        各模块不再自己 try/except 和夹区间，同一键的范围只在这里声明一次。
        """
        raw = (self.get(key, "") or "").strip()
        try:
            value = float(raw) if raw else float(default)
        except ValueError:
            value = float(default)
        if lo is not None:
            value = max(lo, value)
        if hi is not None:
            value = min(hi, value)
        return value

    def int(
        self,
        key: str,
        default: int,
        *,
        lo: int | None = None,
        hi: int | None = None,
    ) -> int:
        """读整数值；允许 `1200.0` 这类写法，先转浮点再取整。"""
        value = self.num(
            key,
            float(default),
            lo=None if lo is None else float(lo),
            hi=None if hi is None else float(hi),
        )
        return int(value)

    def flag(self, key: str, default: bool = True) -> bool:
        """读开关：`0/false/no/off` 为关，其余（含未配置）按 `default`。"""
        raw = (self.get(key, "") or "").strip().lower()
        return default if not raw else raw not in {"0", "false", "no", "off"}

    @property
    def data_dir(self) -> Path:
        return self._path("DATA_DIR", "data")

    @property
    def notes_dir(self) -> Path | None:
        value = self.get("NOTES_DIR", "")
        return Path(value).expanduser() if value else None

    @property
    def in_device(self) -> str:
        """采集设备：`AUDIO_IN_DEVICE` → 旧的 `AUDIO_DEVICE` → `default`。"""
        return self._device("AUDIO_IN_DEVICE")

    @property
    def out_device(self) -> str:
        """播放设备：`AUDIO_OUT_DEVICE` → 旧的 `AUDIO_DEVICE` → `default`。"""
        return self._device("AUDIO_OUT_DEVICE")

    @property
    def out_volume(self) -> int:
        """播放端总音量百分比 0~200：100 为原始，>100 软件放大；缺省 100。"""
        return self.int("AUDIO_OUT_VOLUME", 100, lo=0, hi=200)

    @property
    def outbound_allowlist(self) -> list[str]:
        raw = self.get("OUTBOUND_ALLOWLIST", "") or ""
        return [item.strip() for item in raw.split(",") if item.strip()]

    def _device(self, key: str) -> str:
        return self.get(key, "") or self.get("AUDIO_DEVICE", "") or "default"

    def _path(self, key: str, default: str) -> Path:
        value = self.get(key, default) or default
        path = Path(value).expanduser()
        return path if path.is_absolute() else self.root / path


@dataclass
class Registry:
    """`config/providers.toml` 的只读视图。"""

    root: Path
    defaults: dict = field(default_factory=dict)
    wakewords: list[WakewordEntry] = field(default_factory=list)
    providers: dict[str, dict] = field(default_factory=dict)
    by_word: dict[str, WakewordEntry] = field(default_factory=dict)

    @classmethod
    def load(cls, root: Path = ROOT) -> "Registry":
        path = root / "config" / "providers.toml"
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        entries = [
            WakewordEntry(**{**item, "aliases": tuple(item.get("aliases") or ())})
            for item in data.get("wakewords", [])
        ]
        providers = data.get("providers", {})

        for name, info in providers.items():
            if not str(info.get("pipeline", "")).strip():
                raise ValueError(
                    f"Provider {name!r} 缺少 pipeline 字段（写成 sessions/<链路>）"
                )

        by_word: dict[str, WakewordEntry] = {}
        for entry in entries:
            if entry.link not in VALID_LINKS:
                raise ValueError(f"唤醒词 {entry.word!r} 的 link 非法：{entry.link!r}")
            if entry.provider not in providers:
                raise ValueError(
                    f"唤醒词 {entry.word!r} 引用了未定义的 provider：{entry.provider!r}"
                )
            if entry.word in by_word:
                raise ValueError(f"唤醒词重复：{entry.word!r}")
            for alias in entry.aliases:
                if not alias.strip():
                    raise ValueError(f"唤醒词 {entry.word!r} 有空变体")
                if alias in by_word or alias == entry.word:
                    raise ValueError(
                        f"唤醒词 {entry.word!r} 的变体 {alias!r} 与已有唤醒词重名"
                    )
            by_word[entry.word] = entry

        return cls(
            root=root,
            defaults=data.get("defaults", {}),
            wakewords=entries,
            providers=providers,
            by_word=by_word,
        )

    @property
    def group_names(self) -> list[str]:
        return sorted({entry.config_group for entry in self.wakewords})

    @property
    def wakeword_aliases(self) -> dict[str, list[str]]:
        """唤醒词 → 其它写法，供监听器做容错匹配。"""
        return {entry.word: list(entry.aliases) for entry in self.wakewords}

    def resolve(self, word: str) -> WakewordEntry | None:
        return self.by_word.get(word)

    def pipeline_for(self, provider: str) -> str:
        """该 Provider 对应的会话模板名（`config/providers.toml` 的 `pipeline`）。"""
        info = self.providers.get(provider) or {}
        pipeline = str(info.get("pipeline", "")).strip()
        if not pipeline:
            raise ValueError(f"Provider {provider!r} 未声明 pipeline")
        return pipeline
