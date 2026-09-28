#!/usr/bin/env python3
"""Синхронизация памяти: выгрузить свежие сессии агентов и обновить индекс QMD.

  python3 sync.py                      выгрузка новых сессий, qmd update, qmd embed
  python3 sync.py --detach             то же в фоне, сразу вернуть управление (для хуков)
  python3 sync.py --no-embed           без пересчёта векторов (быстро; поиск по словам свежий)
  python3 sync.py --source hook|timer  кто запустил; пишется в лог и в last-run
  python3 sync.py --status             где лог, когда был последний прогон и чем кончился

Один и тот же скрипт зовут хук конца сессии агента и планировщик системы (launchd,
systemd, Планировщик заданий Windows). Два прогона одновременно не идут. Каждый запуск
сначала оставляет отметку «есть работа», потом пробует взять замок. Кто взял, работает,
пока отметка появляется снова, а после освобождения замка проверяет её ещё раз. Кто не
взял, выходит: его отметку подхватит работающий. Замок держит сама система и снимает,
когда процесс завершился, поэтому упавший прогон не оставляет брошенного замка.

Пути и выгрузчики настраиваются переменными окружения:
  AGENT_MEMORY_DIR     корень выгрузок и состояния, по умолчанию ~/agent-memory
  QMD_BIN              путь к qmd, если он не находится сам
  SYNC_EXPORTERS       через запятую: cc,codex (по умолчанию оба; отсутствующий агент пропускается)
Только стандартная библиотека.
"""

from __future__ import annotations

import argparse
import glob
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
MEMORY_DIR = Path(os.environ.get("AGENT_MEMORY_DIR", Path.home() / "agent-memory")).expanduser()
STATE_DIR = MEMORY_DIR / ".sync"
LOG = STATE_DIR / "sync.log"
LOCK = STATE_DIR / "sync.lock"
PENDING = STATE_DIR / "pending"
LAST = STATE_DIR / "last-run"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_KEEP_LINES = 2000

EXPORTERS = {
    "cc": ("cc_history_to_md.py", "cc-history"),
    "codex": ("codex_history_to_md.py", "codex-history"),
}

# Планировщики запускают задачи с урезанным PATH, где нет ни node, ни qmd. Эти каталоги
# добавляются в PATH дочерних процессов; лишние на этой машине просто не существуют.
EXTRA_BIN_DIRS = [
    "~/.local/bin", "~/.bun/bin", "~/.npm-global/bin", "~/.volta/bin",
    "~/.local/share/fnm/aliases/default/bin", "/opt/homebrew/bin", "/usr/local/bin",
    "~/AppData/Roaming/npm",
]
# Версии Node под nvm лежат в каталогах с номером версии; берутся все, новые первыми.
EXTRA_BIN_GLOBS = ["~/.nvm/versions/node/*/bin"]


def log(message: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n")


def rotate_log() -> None:
    try:
        if LOG.stat().st_size > LOG_MAX_BYTES:
            lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-LOG_KEEP_LINES:]
            LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    extra = [os.path.expanduser(p) for p in EXTRA_BIN_DIRS]
    for pattern in EXTRA_BIN_GLOBS:
        extra += sorted(glob.glob(os.path.expanduser(pattern)), reverse=True)
    qmd_bin = os.environ.get("QMD_BIN")
    if qmd_bin:
        extra.insert(0, str(Path(qmd_bin).expanduser().parent))
    env["PATH"] = os.pathsep.join([*extra, env.get("PATH", "")])
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def find_qmd(env: dict[str, str]) -> str | None:
    explicit = os.environ.get("QMD_BIN")
    if explicit:
        path = Path(explicit).expanduser()
        return str(path) if path.exists() else None
    return shutil.which("qmd", path=env["PATH"])


def try_lock():
    """Неблокирующий замок силами системы. Возвращает открытый файл или None, если занят."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def release_lock(handle) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def run(cmd: list[str], env: dict[str, str], label: str) -> bool:
    log(f"START {label}")
    try:
        result = subprocess.run(cmd, env=env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
    except OSError as exc:
        log(f"END {label}: не запустился: {exc}")
        return False
    output = (result.stdout + result.stderr).strip()
    if output:
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(output[-4000:] + "\n")
    log(f"END {label} exit={result.returncode}")
    return result.returncode == 0


def one_pass(embed: bool) -> bool:
    env = child_env()
    ok = True
    wanted = [x.strip() for x in os.environ.get("SYNC_EXPORTERS", "cc,codex").split(",") if x.strip()]
    for key in wanted:
        if key not in EXPORTERS:
            log(f"неизвестный выгрузчик {key}, пропущен")
            continue
        script, folder = EXPORTERS[key]
        ok &= run([sys.executable, str(HERE / script), "--output", str(MEMORY_DIR / folder)],
                  env, f"export {key}")
    qmd = find_qmd(env)
    if not qmd:
        log("qmd не найден: выгрузка сделана, индекс не обновлён. Задай QMD_BIN.")
        return False
    ok &= run([qmd, "update"], env, "qmd update")
    if embed:
        ok &= run([qmd, "embed"], env, "qmd embed")
    return ok


def sync(embed: bool, source: str) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    rotate_log()
    # Отметка ставится до попытки взять замок. Тогда работающий прогон либо увидит её
    # в своём цикле, либо после освобождения замка, и событие не теряется.
    PENDING.touch()
    ok = True
    ran = False
    while PENDING.exists():
        handle = try_lock()
        if handle is None:
            if not ran:
                log(f"source={source}: идёт другой прогон, он подхватит эту работу")
            break
        try:
            while PENDING.exists():
                PENDING.unlink(missing_ok=True)
                log(f"START sync source={source}")
                ok = one_pass(embed)
                ran = True
                LAST.write_text(f"{datetime.now():%Y-%m-%d %H:%M:%S} source={source} "
                                f"{'ok' if ok else 'error'}\n", encoding="utf-8")
        finally:
            release_lock(handle)
    return 0 if ok else 1


def detach(extra_args: list[str]) -> int:
    """Запустить себя в фоне и сразу вернуть управление: хук не должен ждать индексацию."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(Path(__file__).resolve()), *extra_args]
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)
    return 0


def status() -> int:
    print(f"каталог памяти: {MEMORY_DIR}")
    print(f"лог: {LOG}")
    print(f"последний прогон: {LAST.read_text(encoding='utf-8').strip() if LAST.exists() else 'не было'}")
    handle = try_lock()
    if handle is not None:
        release_lock(handle)
    print(f"идёт прогон: {'нет' if handle is not None else 'да'}")
    qmd = find_qmd(child_env())
    print(f"qmd: {qmd or 'не найден (задай QMD_BIN)'}")
    for key, (_, folder) in EXPORTERS.items():
        path = MEMORY_DIR / folder
        count = sum(1 for _ in path.rglob("*.md")) if path.exists() else 0
        print(f"{folder}: {count} файлов в {path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--detach", action="store_true", help="уйти в фон и сразу вернуть управление")
    parser.add_argument("--no-embed", action="store_true", help="не пересчитывать векторы")
    parser.add_argument("--source", default="manual",
                        help="кто запустил: hook, timer или manual; пишется в лог и в last-run")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.status:
        return status()
    if args.detach:
        # Хук агента может передать JSON события на stdin; он не нужен, но и мешать не должен.
        return detach(["--source", args.source] + (["--no-embed"] if args.no_embed else []))
    return sync(embed=not args.no_embed, source=args.source)


if __name__ == "__main__":
    raise SystemExit(main())
