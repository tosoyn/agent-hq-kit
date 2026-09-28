#!/usr/bin/env python3
"""Выгрузка истории сессий Claude Code в markdown для QMD.

Читает JSONL-сессии из ~/.claude/projects (и из профилей ~/.claude-*/projects, если
они есть), отбрасывает служебный шум и пишет по одному .md на сессию с YAML-шапкой
контракта (memory/contract.py). Только стандартная библиотека.

  python3 cc_history_to_md.py                  все новые и изменившиеся сессии
  python3 cc_history_to_md.py --since 2026-09-01
  python3 cc_history_to_md.py --last           самая свежая сессия
  python3 cc_history_to_md.py --transcript P   ровно этот JSONL
  python3 cc_history_to_md.py --dry-run        показать, что будет сделано

Куда писать: --output, иначе переменная CC_HISTORY_OUTPUT, иначе ~/agent-memory/cc-history.
"""

from __future__ import annotations

import json
import os
import re
import sys
from argparse import ArgumentParser
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from contract import (  # noqa: E402
    contract_lines, is_ephemeral_cwd, is_fresh, project_name_from_cwd, raw_exists, write_atomic,
    yaml_quote,
)


def claude_project_dirs() -> list[Path]:
    """Каталоги projects всех профилей Claude Code на этой машине."""
    candidates = set(Path.home().glob(".claude*/projects"))
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        candidates.add(Path(config_dir).expanduser() / "projects")
    return sorted(p for p in candidates if p.is_dir())


OUTPUT_DIR = Path(os.environ.get("CC_HISTORY_OUTPUT", Path.home() / "agent-memory" / "cc-history"))
# Сессия короче двух сообщений ничего не расскажет поиску.
MIN_MESSAGES = 2
# Потолок одного ответа агента в выгрузке. Больше — тяжелее индекс и дороже каждое
# чтение из памяти; меньше — у длинного разбора теряется самая содержательная часть.
# Обрезка честно помечается полем truncated, полный текст всегда остаётся в исходнике.
MAX_ASSISTANT_CHARS = 12000
# Очень большие транскрипты (обычно с картинками) в пакетном режиме пропускаются:
# их разбор медленный. Режимы --last и --transcript этот предел не применяют.
MAX_FILE_SIZE_MB = 15

TAG_KEYWORDS = {
    "debug": ["ошибк", "error", "bug", "fix", "crash", "fail", "broken", "баг", "фикс"],
    "deploy": ["deploy", "docker", "endpoint", "production", "деплой"],
    "ui": ["компонент", "component", "layout", "css", "tailwind", "дизайн", "кнопк"],
    "api": ["api", "endpoint", "request", "response", "fetch"],
    "refactor": ["refactor", "рефактор", "переименов", "rename", "extract"],
    "config": ["config", "конфиг", "настройк", "settings", ".env"],
    "test": ["test", "тест", "pytest", "vitest", "playwright", "e2e", "mock"],
    "git": ["commit", "branch", "merge", "pull request", "push"],
    "research": ["research", "исследован", "попроб", "эксперимент"],
    "planning": ["roadmap", "план", "plan", "milestone", "спек"],
}

SERVICE_TAGS = re.compile(
    r"<(command-message|command-name|command-args|local-command-stdout|local-command-caveat"
    r"|system-reminder|available-deferred-tools|task-notification)>.*?</\1>",
    re.DOTALL,
)


def clean_text(text: str) -> str:
    return SERVICE_TAGS.sub("", text or "").strip()


def is_real_user_message(record: dict) -> bool:
    """Отличает ввод человека от результата инструмента."""
    if "sourceToolAssistantUUID" in record:
        return False
    content = record.get("message", {}).get("content", "")
    if isinstance(content, list):
        return not any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content)
    return True


def tool_description(name: str, inp: dict) -> str:
    if name == "Bash":
        cmd = str(inp.get("command", ""))
        return cmd[:80] + ("..." if len(cmd) > 80 else "")
    if name in ("Read", "Edit", "Write"):
        return Path(str(inp.get("file_path", ""))).name
    if name in ("Glob", "Grep"):
        return str(inp.get("pattern", ""))[:60]
    if name == "Agent":
        return str(inp.get("description", ""))[:60]
    if name == "Skill":
        return str(inp.get("skill", ""))
    if name == "WebFetch":
        return str(inp.get("url", ""))[:120]
    return ""


def parse_session(path: Path) -> dict:
    messages: list[dict] = []
    tools: Counter = Counter()
    models: set[str] = set()
    tokens = 0
    first_ts = last_ts = None
    cwd = branch = session_id = None
    truncated = False
    # Вопрос владельцу с вариантами живёт в вызове инструмента, а ответ в его результате.
    # Пара склеивается, иначе из истории пропадает, что владелец выбрал и от чего отказался.
    pending_asks: dict[str, str] = {}

    def add(role: str, text: str, ts) -> None:
        nonlocal truncated
        if role == "assistant" and len(text) > MAX_ASSISTANT_CHARS:
            truncated = True
            text = text[:MAX_ASSISTANT_CHARS] + "\n\n[...обрезано, полный текст в исходнике...]"
        messages.append({"role": role, "text": text, "ts": ts})

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue

            ts = None
            if record.get("timestamp"):
                try:
                    ts = datetime.fromisoformat(str(record["timestamp"]).replace("Z", "+00:00"))
                    first_ts = first_ts or ts
                    last_ts = ts
                except ValueError:
                    pass
            session_id = session_id or record.get("sessionId")
            cwd = cwd or record.get("cwd")
            branch = branch or record.get("gitBranch")

            kind = record.get("type", "")
            if kind in ("progress", "file-history-snapshot", "system"):
                continue
            msg = record.get("message") or {}
            content = msg.get("content", "")
            if msg.get("model") and msg["model"] != "<synthetic>":
                models.add(msg["model"])
            usage = msg.get("usage") or {}
            tokens += int(usage.get("input_tokens", 0) or 0) + int(usage.get("output_tokens", 0) or 0)

            if kind == "user" and isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    ask = pending_asks.pop(block.get("tool_use_id"), None)
                    body = block.get("content")
                    if isinstance(body, list):
                        body = " ".join(x.get("text", "") for x in body if isinstance(x, dict))
                    body = body if isinstance(body, str) else ""
                    if ask and body.strip():
                        add("user", f"{ask}\n[ОТВЕТ ВЛАДЕЛЬЦА] {body.strip()[:2000]}", ts)
                    elif "Request interrupted" in body:
                        add("user", "[ВЛАДЕЛЕЦ ПРЕРВАЛ РАБОТУ]", ts)

            if kind == "user" and msg.get("role") == "user" and is_real_user_message(record):
                text = content if isinstance(content, str) else " ".join(
                    c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
                text = clean_text(text)
                if text:
                    add("user", text, ts)

            elif kind == "assistant" and msg.get("role") == "assistant":
                parts: list[str] = []
                blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        name = block.get("name", "tool")
                        tools[name] += 1
                        inp = block.get("input") or {}
                        if name == "AskUserQuestion":
                            lines = ["[ВОПРОС ВЛАДЕЛЬЦУ]"]
                            for q in inp.get("questions") or []:
                                options = "; ".join(str(o.get("label", "")) for o in q.get("options") or [])
                                lines.append(f"  {q.get('question', '')}\n  варианты: {options}")
                            ask_text = "\n".join(lines)
                            pending_asks[block.get("id")] = ask_text
                            parts.append(ask_text)
                        else:
                            desc = tool_description(name, inp)
                            if desc:
                                parts.append(f"[{name}: {desc}]")
                text = clean_text("\n".join(parts))
                if text:
                    add("assistant", text, ts)

    return {
        "session_id": session_id or path.stem,
        "raw_path": path,
        "messages": messages,
        "tools": dict(tools.most_common()),
        "models": sorted(models),
        "tokens": tokens,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "cwd": cwd,
        "branch": branch,
        "truncated": truncated,
    }


def format_duration(seconds: int) -> str:
    if seconds >= 3600:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def auto_tags(session: dict, project: str) -> list[str]:
    tags = {"claude-code", project.lower().replace(" ", "-")}
    names = set(session["tools"])
    if names & {"Edit", "Write"}:
        tags.add("code-changes")
    if "Agent" in names:
        tags.add("agents")
    corpus = " ".join(m["text"].lower() for m in session["messages"])
    tags.update(tag for tag, words in TAG_KEYWORDS.items() if any(w in corpus for w in words))
    return sorted(tags)


def format_markdown(session: dict, project_key: str) -> str:
    msgs = session["messages"]
    project = project_name_from_cwd(session["cwd"]) if session["cwd"] else project_key
    first_user = next((m["text"] for m in msgs if m["role"] == "user"), "Session")
    title = first_user[:100].replace("\n", " ").strip() + ("..." if len(first_user) > 100 else "")
    first = session["first_ts"]
    date_str = first.strftime("%Y-%m-%d") if first else "unknown"
    time_str = first.strftime("%H:%M") if first else ""
    duration = 0
    if first and session["last_ts"]:
        duration = int((session["last_ts"] - first).total_seconds())
    top_tools = ", ".join(f"{n}({c})" for n, c in list(session["tools"].items())[:5])
    raw = session["raw_path"]

    lines = ["---", f'title: "{yaml_quote(title)}"']
    lines += contract_lines(
        source="cc", session_id=session["session_id"], project=project, project_key=project_key,
        cwd=session["cwd"] or "", started_at=first, updated_at=session["last_ts"],
        raw_locator=raw, raw_available=raw_exists(raw), truncated=session["truncated"],
    )
    lines += [
        f"date: {date_str}",
        f'time: "{time_str}"',
        f'duration: "{format_duration(duration)}"',
        f"tokens: {session['tokens']}",
        f'model: "{yaml_quote(", ".join(session["models"]) or "unknown")}"',
    ]
    if session["branch"] and session["branch"] != "HEAD":
        lines.append(f'branch: "{yaml_quote(session["branch"])}"')
    lines += [f'tools: "{yaml_quote(top_tools)}"', f"tags: [{', '.join(auto_tags(session, project))}]", "---", "",
              f"# {title}", "",
              f"> **Проект:** {project} | **Дата:** {date_str} {time_str} | "
              f"**Длительность:** {format_duration(duration)}", ""]

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
    return "\n".join(lines)


def export(transcript: Path, output: Path, *, force: bool = False, dry_run: bool = False) -> bool:
    """Выгружает одну сессию. True, если файл записан (или был бы записан в dry-run)."""
    project_key = transcript.parent.name
    target = output / project_key / f"{transcript.stem}.md"
    if not force and is_fresh(target, transcript):
        return False
    try:
        source_mtime_ns = transcript.stat().st_mtime_ns
        session = parse_session(transcript)
    except OSError as exc:
        print(f"  ошибка чтения {transcript}: {exc}", file=sys.stderr)
        return False
    if len(session["messages"]) < MIN_MESSAGES or is_ephemeral_cwd(session["cwd"]):
        return False
    if dry_run:
        print(f"  {transcript} -> {target}")
        return True
    write_atomic(target, format_markdown(session, project_key), source_mtime_ns)
    return True


def all_transcripts() -> list[Path]:
    # Файлы субагентов лежат глубже, в подкаталогах; берём только сессии верхнего уровня.
    return [f for base in claude_project_dirs() for d in base.iterdir() if d.is_dir()
            for f in d.glob("*.jsonl")]


def main() -> int:
    parser = ArgumentParser(description="Выгрузка истории Claude Code в markdown для QMD")
    parser.add_argument("--output", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--since", help="только сессии, менявшиеся с этой даты (YYYY-MM-DD)")
    parser.add_argument("--last", action="store_true", help="только самая свежая сессия")
    parser.add_argument("--transcript", type=Path, help="ровно этот JSONL")
    parser.add_argument("--force", action="store_true", help="переписать, даже если выгрузка свежая")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.transcript:
        targets = [args.transcript]
    else:
        transcripts = all_transcripts()
        if not transcripts:
            print("Сессий Claude Code не найдено (~/.claude/projects).")
            return 0
        if args.last:
            targets = [max(transcripts, key=lambda p: p.stat().st_mtime)]
        else:
            targets = [p for p in transcripts if p.stat().st_size <= MAX_FILE_SIZE_MB * 1024 * 1024]
            if args.since:
                since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
                targets = [p for p in targets if p.stat().st_mtime >= since]

    exported = sum(export(p, args.output, force=args.force, dry_run=args.dry_run) for p in sorted(targets))
    print(f"exported {exported} -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
