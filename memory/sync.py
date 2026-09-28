#!/usr/bin/env python3
"""Синхронизация памяти: выгрузить свежие сессии агентов и обновить индекс QMD.

  python3 sync.py              выгрузка всех новых сессий, qmd update, qmd embed
  python3 sync.py --detach     то же в фоне, сразу вернуть управление (для хуков)
  python3 sync.py --no-embed   без пересчёта векторов (быстро; поиск по словам уже свежий)
  python3 sync.py --status     где лог, когда был последний прогон и чем кончился

Один и тот же скрипт зовут хук конца сессии агента и планировщик системы (launchd,
systemd, Планировщик заданий Windows). Два прогона одновременно не идут: второй
оставляет отметку «есть работа» и выходит, а первый перед завершением проходит ещё
раз. Так событие не теряется и тяжёлый пересчёт векторов не запускается дважды.

Пути и выгрузчики настраиваются переменными окружения:
  AGENT_MEMORY_DIR     корень выгрузок и состояния, по умолчанию ~/agent-memory
  QMD_BIN              путь к qmd, если он не находится сам
  SYNC_EXPORTERS       через запятую: cc,codex (по умолчанию оба; отсутствующий агент пропускается)
Только стандартная библиотека.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
MEMORY_DIR = Path(os.environ.get("AGENT_MEMORY_DIR", Path.home() / "agent-memory")).expanduser()
STATE_DIR = MEMORY_DIR / ".sync"
LOG = STATE_DIR / "sync.log"
LOCK = STATE_DIR / "sync.lock"
PENDING = STATE_DIR / "pending"
LAST = STATE_DIR / "last-run"
# Замок старше этого срока считается брошенным упавшим прогоном.
STALE_LOCK_SECONDS = 2 * 60 * 60
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
    "/opt/homebrew/bin", "/usr/local/bin", "~/AppData/Roaming/npm",
]


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
    qmd_bin = os.environ.get("QMD_BIN")
    if qmd_bin:
        extra.insert(0, str(Path(qmd_bin).expanduser().parent))
    env["PATH"] = os.pathsep.join([*extra, env.get("PATH", "")])
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def find_qmd(env: dict[str, str]) -> str | None:
    explicit = os.environ.get("QMD_BIN")
    if explicit:
        return explicit if Path(explicit).expanduser().exists() else None
    return shutil.which("qmd", path=env["PATH"])


def acquire_lock() -> bool:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {time.time():.0f}\n".encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                age = time.time() - LOCK.stat().st_mtime
            except OSError:
                continue
            if age < STALE_LOCK_SECONDS:
                return False
            log(f"снят брошенный замок возрастом {int(age)} с")
            LOCK.unlink(missing_ok=True)
    return False


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


def sync(embed: bool) -> int:
    rotate_log()
    if not acquire_lock():
        PENDING.touch()
        log("идёт другой прогон; отметка оставлена, он пройдёт ещё раз")
        return 0
    ok = True
    try:
        # Отметка, оставленная во время прогона, значит, что после его начала закончилась
        # ещё одна сессия. Три круга хватает, чтобы догнать пачку соседних событий.
        for _ in range(3):
            PENDING.unlink(missing_ok=True)
            ok = one_pass(embed)
            if not PENDING.exists():
                break
    finally:
        LOCK.unlink(missing_ok=True)
    LAST.write_text(f"{datetime.now():%Y-%m-%d %H:%M:%S} {'ok' if ok else 'error'}\n", encoding="utf-8")
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
    print(f"идёт прогон: {'да' if LOCK.exists() else 'нет'}")
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
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.status:
        return status()
    if args.detach:
        # Хук агента может передать JSON события на stdin; он не нужен, но и мешать не должен.
        return detach(["--no-embed"] if args.no_embed else [])
    return sync(embed=not args.no_embed)


if __name__ == "__main__":
    raise SystemExit(main())
