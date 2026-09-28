"""Общий контракт метаданных для выгрузок истории сессий в QMD.

Выгрузка (проекция) это поисковый след сессии, а не сам транскрипт. Доказательный
источник остаётся у агента: JSONL-файл сессии. Поля ниже дают дорогу от документа,
найденного в QMD, обратно к исходнику, поэтому их набор и написание одинаковы у всех
экспортёров. Новый экспортёр для другого агента импортирует этот модуль и пишет
шапку через contract_lines().

Модуль не тянет ничего, кроме стандартной библиотеки, и не знает, кто его вызвал.
"""

from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

# Версия растёт, когда меняется набор или смысл полей, а не их значения.
CONTRACT_VERSION = "qmd-history/1"

# Команда нативного продолжения сессии по источнику. Новый агент добавляется сюда.
RESUME_COMMANDS = {
    "cc": "claude --resume {session_id}",
    "codex": "codex resume {session_id}",
}

REQUIRED_FIELDS = (
    "source",
    "session_id",
    "project",
    "project_key",
    "cwd",
    "started_at",
    "updated_at",
    "raw_locator",
    "raw_available",
    "native_resume",
    "truncated",
    "contract",
)

# Эфемерные корни: каталоги стендов, пробных прогонов и временных копий. Сессия,
# запущенная там, в память не попадает, так эксперимент не засоряет поиск синтетикой.
# Список переопределяется переменной окружения через системный разделитель путей.
DEFAULT_EPHEMERAL_ROOTS = ("~/.cache", "/tmp", "/var/tmp", "/private/tmp",
                           "~/AppData/Local/Temp", tempfile.gettempdir())
EPHEMERAL_ROOTS_ENV = "AGENT_EPHEMERAL_ROOTS"


def ephemeral_roots() -> list[Path]:
    raw = os.environ.get(EPHEMERAL_ROOTS_ENV)
    parts = [x for x in raw.split(os.pathsep) if x.strip()] if raw else DEFAULT_EPHEMERAL_ROOTS
    roots = []
    for part in parts:
        try:
            roots.append(Path(os.path.realpath(os.path.expanduser(part.strip()))))
        except OSError:
            continue
    return roots


def is_ephemeral_cwd(cwd: object) -> bool:
    """Лежит ли рабочий каталог сессии под эфемерным корнем.

    Сравниваются разрешённые пути, поэтому `/tmpfoo` под `/tmp` не попадает, а симлинк
    на `/tmp` попадает (на macOS `/tmp` это ссылка на `/private/tmp`). Пустой cwd
    эфемерным не считается: неизвестное не выбрасывают.
    """
    if not cwd:
        return False
    try:
        path = Path(os.path.realpath(os.path.expanduser(str(cwd))))
    except OSError:
        return False
    return any(path == root or path.is_relative_to(root) for root in ephemeral_roots())


# Секрет, вставленный в чат, иначе попал бы в выгрузку и в индекс. Паттерны те же, что
# у секрет-гейта в .githooks/pre-commit. Это страховка, а не гарантия: ключ в
# нестандартном формате паттерн не узнает.
SECRET_PATTERN = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"
    r"|BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY"
    r"|AKIA[0-9A-Z]{16}"
    r"|sk-(?:ant|proj|live|test)-[A-Za-z0-9_-]{20,}"
    r"|xox[bap]-[A-Za-z0-9-]{10,}"
    r"|gh[pousr]_[A-Za-z0-9]{30,}"
    r"|github_pat_[A-Za-z0-9_]{30,}"
    r"|AIza[0-9A-Za-z_-]{30,}",
    re.DOTALL,
)


def redact(text: str) -> str:
    return SECRET_PATTERN.sub("[СЕКРЕТ СКРЫТ]", text or "")


def local_time(ts: datetime | None) -> datetime | None:
    """Время записи в часовом поясе этой машины: сессию ищут по «вчера утром», а не по UTC."""
    return ts.astimezone() if ts is not None and ts.tzinfo is not None else ts


def yaml_quote(value: object) -> str:
    """Содержимое строки в двойных кавычках для YAML-шапки."""
    text = "" if value is None else str(value)
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    return text.replace("\n", " ").replace("\r", " ")


def to_iso(value: object) -> str:
    """ISO 8601 или пустая строка. Пустая строка честнее выдуманного времени."""
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value).isoformat()
        except (OSError, ValueError, OverflowError):
            return ""
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
        except ValueError:
            return ""
    return ""


def raw_exists(path: object) -> bool:
    """Есть ли исходник на этой машине прямо сейчас.

    История, перенесённая с другой машины, индексируется, но её JSONL здесь
    недоступен: такая выгрузка обязана сказать это полем, а не притвориться
    первоисточником.
    """
    if not path:
        return False
    try:
        return Path(path).is_file()
    except OSError:
        return False


def native_resume_command(source: str, session_id: str) -> str:
    template = RESUME_COMMANDS.get(source)
    return template.format(session_id=session_id) if template and session_id else ""


def project_key_from_cwd(cwd: object) -> str:
    """Ключ проекта из рабочего каталога: путь без разделителей."""
    if not cwd:
        return "unknown-project"
    normalized = str(cwd).replace("\\", "-").replace("/", "-").replace(":", "")
    while "--" in normalized:
        normalized = normalized.replace("--", "-")
    return normalized.strip("-") or "unknown-project"


def project_name_from_cwd(cwd: object) -> str:
    """Читаемое имя проекта: последняя часть пути рабочего каталога."""
    if not cwd:
        return "unknown"
    path = Path(str(cwd))
    if path == Path.home():
        return "home"
    return path.name or str(path)


def contract_lines(
    *,
    source: str,
    session_id: str,
    project: str = "",
    project_key: str = "",
    cwd: str = "",
    started_at: object = None,
    updated_at: object = None,
    raw_locator: object = "",
    raw_available: bool = False,
    native_resume: str | None = None,
    truncated: bool = False,
) -> list[str]:
    """Строки YAML-шапки контракта, одинаковые у всех экспортёров."""
    if native_resume is None:
        # Продолжить можно только сессию, чей исходник лежит на этой машине.
        native_resume = native_resume_command(source, session_id) if raw_available else ""
    return [
        f"source: {source}",
        f'session_id: "{yaml_quote(session_id)}"',
        f'project: "{yaml_quote(project)}"',
        f'project_key: "{yaml_quote(project_key)}"',
        f'cwd: "{yaml_quote(cwd)}"',
        f'started_at: "{to_iso(started_at)}"',
        f'updated_at: "{to_iso(updated_at)}"',
        f'raw_locator: "{yaml_quote(os.fspath(raw_locator) if raw_locator else "")}"',
        f"raw_available: {'true' if raw_available else 'false'}",
        f'native_resume: "{yaml_quote(native_resume)}"',
        f"truncated: {'true' if truncated else 'false'}",
        f"contract: {CONTRACT_VERSION}",
    ]


def read_frontmatter(path: object) -> dict[str, str]:
    """Плоский разбор YAML-шапки выгрузки без внешних зависимостей."""
    fields: dict[str, str] = {}
    try:
        handle = Path(path).open("r", encoding="utf-8")
    except OSError:
        return fields
    with handle:
        if handle.readline().strip() != "---":
            return fields
        for line in handle:
            stripped = line.strip()
            if stripped == "---":
                break
            if not stripped or stripped.startswith("#") or ":" not in stripped:
                continue
            key, _, value = stripped.partition(":")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            fields[key.strip()] = value
    return fields


def write_atomic(target: Path, text: str, source_mtime_ns: int | None = None) -> None:
    """Запись через временный файл. Время изменения ставится по исходнику.

    Живая сессия продолжает дописываться. Если бы выгрузка несла своё время записи,
    она выглядела бы свежее исходника, выросшего во время разбора, и проверка
    «уже выгружено» пропускала бы её навсегда. Время исходника, прочитанное до
    разбора, делает выросший исходник строго новее, и следующий прогон его повторит.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(target)
    if source_mtime_ns is not None:
        try:
            os.utime(target, ns=(source_mtime_ns, source_mtime_ns))
        except OSError:
            pass


def is_fresh(target: Path, source: Path) -> bool:
    """Выгрузка не старше исходника: переделывать не нужно."""
    try:
        return target.stat().st_mtime_ns >= source.stat().st_mtime_ns
    except OSError:
        return False
