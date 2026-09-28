#!/usr/bin/env python3
"""Выгрузка истории сессий Codex CLI в markdown для QMD.

Читает rollout-*.jsonl из ~/.codex/sessions (или $CODEX_HOME/sessions), отбрасывает
вставки клиента (инструкции проекта, описание окружения) и пишет по одному .md на
сессию с YAML-шапкой контракта (memory/contract.py). Только стандартная библиотека.

  python3 codex_history_to_md.py            все новые и изменившиеся сессии
  python3 codex_history_to_md.py --last     самая свежая сессия
  python3 codex_history_to_md.py --dry-run

Куда писать: --output, иначе CODEX_HISTORY_OUTPUT, иначе ~/agent-memory/codex-history.
"""

from __future__ import annotations

import json
import os
import re
import sys
from argparse import ArgumentParser
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from contract import (  # noqa: E402
    contract_lines, is_ephemeral_cwd, is_fresh, project_key_from_cwd, project_name_from_cwd,
    raw_exists, write_atomic, yaml_quote,
)

CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
SESSIONS_DIR = CODEX_HOME / "sessions"
OUTPUT_DIR = Path(os.environ.get("CODEX_HISTORY_OUTPUT", Path.home() / "agent-memory" / "codex-history"))
MIN_MESSAGES = 2
# Тот же потолок и тот же смысл, что у выгрузки Claude Code.
MAX_ASSISTANT_CHARS = 12000
MAX_TOOL_ACTIVITY = 30

TAG_KEYWORDS = {
    "debug": ["ошибк", "error", "bug", "fix", "crash", "fail", "баг", "фикс"],
    "deploy": ["deploy", "docker", "endpoint", "production", "деплой"],
    "config": ["config", "конфиг", "settings", ".toml", ".env"],
    "review": ["review", "ревью", "audit", "аудит"],
    "test": ["test", "тест", "pytest", "vitest"],
}

# Блоки, которые клиент подставляет от имени пользователя. Настоящий ввод приходит
# рядом с ними в том же сообщении, поэтому фильтр работает поблочно.
SKIP_USER_PREFIXES = (
    "<environment_context>",
    "<permissions instructions>",
    "<collaboration_mode>",
    "<apps_instructions>",
    "<skills_instructions>",
    "<recommended_plugins>",
    "<codex_internal_context",
    "<user_instructions>",
    "# AGENTS.md instructions",
    "[Request interrupted by user",
)
SERVICE_TAGS = re.compile(
    r"<(command-message|command-name|command-args|local-command-stdout"
    r"|local-command-caveat|system-reminder|task-notification)>.*?</\1>",
    re.DOTALL,
)


def parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def clean(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").strip()
    return re.sub(r"\n{3,}", "\n\n", text)


def clean_user(text: str) -> str:
    cleaned = clean(SERVICE_TAGS.sub("", text or ""))
    return "" if not cleaned or cleaned.startswith(SKIP_USER_PREFIXES) else cleaned


def blocks_text(content, kinds: set[str], user: bool = False) -> str:
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") in kinds:
            text = clean_user(block.get("text", "")) if user else block.get("text", "")
            if text:
                parts.append(text)
    return clean("\n\n".join(parts))


def tool_description(name: str, raw_args) -> str:
    args = raw_args
    if isinstance(raw_args, str):
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError:
            return " ".join(raw_args.split())[:120]
    if not isinstance(args, dict):
        return ""
    if name in {"shell_command", "shell", "exec_command"}:
        command = args.get("command") or args.get("cmd") or ""
        return (" ".join(map(str, command)) if isinstance(command, list) else str(command))[:120]
    if name == "apply_patch":
        files = re.findall(r"\*\*\* (?:Add|Update|Delete) File: (.+)", str(args.get("patch") or raw_args))
        return ", ".join(files[:4]) or "patch"
    for key in ("path", "query", "q"):
        if key in args:
            return str(args[key])[:120]
    return json.dumps(args, ensure_ascii=False)[:120]


def parse_session(path: Path) -> dict:
    messages: list[dict] = []
    activity: list[dict] = []
    tools: Counter = Counter()
    models: set[str] = set()
    tokens = 0
    first_ts = last_ts = None
    cwd = session_id = None
    truncated = False
    # Один и тот же запрос клиент пишет дважды: событием и сообщением. Счётчик гасит
    # копию, не трогая повтор, который пользователь действительно ввёл дважды.
    pending_user: Counter = Counter()

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            ts = parse_ts(record.get("timestamp"))
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
            kind = record.get("type")
            payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}

            if kind == "session_meta":
                session_id = payload.get("id") or session_id
                cwd = payload.get("cwd") or cwd
            elif kind == "turn_context":
                cwd = payload.get("cwd") or cwd
                if payload.get("model"):
                    models.add(payload["model"])
            elif kind == "event_msg":
                if payload.get("type") == "user_message":
                    text = clean_user(payload.get("message", ""))
                    if text:
                        messages.append({"role": "user", "text": text, "ts": ts})
                        pending_user[text] += 1
                elif payload.get("type") == "token_count":
                    usage = (payload.get("info") or {}).get("total_token_usage") or {}
                    tokens = max(tokens, int(usage.get("total_tokens", 0) or 0))
            elif kind == "response_item":
                ptype, role = payload.get("type"), payload.get("role")
                if ptype == "message" and role == "assistant":
                    text = blocks_text(payload.get("content"), {"output_text", "text"})
                    if text:
                        if len(text) > MAX_ASSISTANT_CHARS:
                            truncated = True
                            text = text[:MAX_ASSISTANT_CHARS] + "\n\n[...обрезано, полный текст в исходнике...]"
                        messages.append({"role": "assistant", "text": text, "ts": ts})
                elif ptype == "message" and role == "user":
                    text = blocks_text(payload.get("content"), {"input_text", "text"}, user=True)
                    if text and pending_user[text] > 0:
                        pending_user[text] -= 1
                    elif text:
                        messages.append({"role": "user", "text": text, "ts": ts})
                elif ptype in ("function_call", "custom_tool_call"):
                    # Свежие версии Codex пишут вызовы как custom_tool_call с текстом
                    # в поле input вместо JSON в arguments.
                    name = payload.get("name", "tool")
                    tools[name] += 1
                    raw_args = payload.get("arguments", payload.get("input"))
                    desc = tool_description(name, raw_args)
                    if desc and len(activity) < MAX_TOOL_ACTIVITY:
                        activity.append({"ts": ts, "name": name, "desc": desc})

    return {
        "session_id": session_id or path.stem,
        "raw_path": path,
        "messages": messages,
        "activity": activity,
        "tools": dict(tools.most_common()),
        "models": sorted(models),
        "tokens": tokens,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "cwd": cwd,
        "truncated": truncated,
    }


def format_duration(seconds: int) -> str:
    if seconds >= 3600:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def format_markdown(session: dict) -> str:
    msgs = session["messages"]
    cwd = session["cwd"]
    project = project_name_from_cwd(cwd)
    first_user = next((m["text"] for m in msgs if m["role"] == "user"), "Codex session")
    title = first_user[:100].replace("\n", " ").strip() + ("..." if len(first_user) > 100 else "")
    first = session["first_ts"]
    date_str = first.strftime("%Y-%m-%d") if first else "unknown"
    time_str = first.strftime("%H:%M") if first else ""
    duration = int((session["last_ts"] - first).total_seconds()) if first and session["last_ts"] else 0
    top_tools = ", ".join(f"{n}({c})" for n, c in list(session["tools"].items())[:5])
    corpus = " ".join(m["text"].lower() for m in msgs)
    tags = {"codex", project.lower().replace(" ", "-")}
    tags.update(t for t, words in TAG_KEYWORDS.items() if any(w in corpus for w in words))
    if "apply_patch" in session["tools"]:
        tags.add("code-changes")
    raw = session["raw_path"]

    lines = ["---", f'title: "{yaml_quote(title)}"']
    lines += contract_lines(
        source="codex", session_id=session["session_id"], project=project,
        project_key=project_key_from_cwd(cwd), cwd=cwd or "", started_at=first,
        updated_at=session["last_ts"], raw_locator=raw, raw_available=raw_exists(raw),
        truncated=session["truncated"],
    )
    lines += [
        f"date: {date_str}",
        f'time: "{time_str}"',
        f'duration: "{format_duration(duration)}"',
        f"tokens: {session['tokens']}",
        f'model: "{yaml_quote(", ".join(session["models"]) or "unknown")}"',
        f'tools: "{yaml_quote(top_tools)}"',
        f"tags: [{', '.join(sorted(tags))}]",
        "---", "",
        f"# {title}", "",
        f"> **Проект:** {project} | **Дата:** {date_str} {time_str} | **Длительность:** {format_duration(duration)}",
        "",
    ]
    for m in msgs:
        stamp = m["ts"].strftime("%H:%M") if m["ts"] else ""
        if m["role"] == "user":
            lines.append((f"## [{stamp}] Q: " if stamp else "## Q: ") + m["text"][:200].replace("\n", " "))
            if len(m["text"]) > 200:
                lines.append(m["text"][200:])
        else:
            if stamp:
                lines += [f"*[{stamp}]*", ""]
            lines.append(m["text"])
        lines.append("")
    if session["activity"]:
        lines += ["## Инструменты", ""]
        for item in session["activity"]:
            stamp = item["ts"].strftime("%H:%M") if item["ts"] else "--:--"
            lines.append(f"- [{stamp}] `{item['name']}`: {item['desc']}")
        lines.append("")
    return "\n".join(lines)


def export(path: Path, output: Path, *, force: bool = False, dry_run: bool = False) -> bool:
    try:
        source_mtime_ns = path.stat().st_mtime_ns
        session = parse_session(path)
    except OSError as exc:
        print(f"  ошибка чтения {path}: {exc}", file=sys.stderr)
        return False
    if len(session["messages"]) < MIN_MESSAGES or is_ephemeral_cwd(session["cwd"]):
        return False
    target = output / project_key_from_cwd(session["cwd"]) / f"{path.stem}.md"
    if not force and is_fresh(target, path):
        return False
    if dry_run:
        print(f"  {path} -> {target}")
        return True
    write_atomic(target, format_markdown(session), source_mtime_ns)
    return True


def exported_mtimes(output: Path) -> dict[str, list[int]]:
    """Имя выгрузки -> время изменения. Каталог выгрузки зависит от cwd сессии, а имя
    файла от исходника, поэтому свежесть можно проверить без разбора всего JSONL."""
    index: dict[str, list[int]] = {}
    for md in output.rglob("*.md") if output.exists() else []:
        try:
            index.setdefault(md.stem, []).append(md.stat().st_mtime_ns)
        except OSError:
            continue
    return index


def main() -> int:
    parser = ArgumentParser(description="Выгрузка истории Codex в markdown для QMD")
    parser.add_argument("--output", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--last", action="store_true", help="только самая свежая сессия")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not SESSIONS_DIR.is_dir():
        print(f"Сессий Codex не найдено ({SESSIONS_DIR}).")
        return 0
    files = sorted(SESSIONS_DIR.rglob("rollout-*.jsonl"))
    if args.last:
        files = files[-1:]  # имя начинается с даты и времени старта сессии
    elif not args.force:
        index = exported_mtimes(args.output)
        files = [f for f in files
                 if not (len(index.get(f.stem, [])) == 1 and index[f.stem][0] >= f.stat().st_mtime_ns)]

    exported = sum(export(f, args.output, force=args.force, dry_run=args.dry_run) for f in files)
    print(f"exported {exported} -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
