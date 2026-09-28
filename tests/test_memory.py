"""Тесты памяти на синтетике. Запуск из корня штаба: python3 -m unittest discover tests

Живые данные не трогаются: сессии, выгрузки и состояние синхронизации создаются во
временных каталогах, вместо настоящего qmd работает подставной скрипт. Нужен только
Python 3.10+.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MEMORY = Path(__file__).resolve().parents[1] / "memory"
sys.path.insert(0, str(MEMORY))

import cc_history_to_md as cc  # noqa: E402
import codex_history_to_md as codex  # noqa: E402
from contract import (  # noqa: E402
    REQUIRED_FIELDS, is_ephemeral_cwd, project_key_from_cwd, read_frontmatter, redact,
)

FAKE_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"  # собран из частей, чтобы секрет-гейт не ругался на тест


def temp_dir(test: unittest.TestCase) -> Path:
    path = Path(tempfile.mkdtemp())
    test.addCleanup(shutil.rmtree, path, ignore_errors=True)
    return path


def write_jsonl(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
    return path


def cc_session(cwd: str, long_answer: bool = False) -> list[dict]:
    base = {"sessionId": "s-1", "cwd": cwd, "gitBranch": "main"}
    answer = "Готово: поправил импорт." + (" x" * 20000 if long_answer else "")
    return [
        {**base, "type": "user", "timestamp": "2026-09-28T10:00:00Z",
         "message": {"role": "user",
                     "content": f"Почини сборку, ключ {FAKE_KEY} <system-reminder>служебное</system-reminder>"}},
        {**base, "type": "assistant", "timestamp": "2026-09-28T10:01:00Z",
         "message": {"role": "assistant", "model": "claude-test", "usage": {"input_tokens": 10, "output_tokens": 5},
                     "content": [{"type": "text", "text": "Смотрю ошибку."},
                                 {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "make build"}},
                                 {"type": "tool_use", "id": "q1", "name": "AskUserQuestion",
                                  "input": {"questions": [{"question": "Чинить сейчас?",
                                                           "options": [{"label": "Да"}, {"label": "Позже"}]}]}}]}},
        {**base, "type": "user", "timestamp": "2026-09-28T10:02:00Z", "sourceToolAssistantUUID": "x",
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "q1", "content": "Да"}]}},
        {**base, "type": "assistant", "timestamp": "2026-09-28T10:03:00Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": answer}]}},
    ]


class CcExporterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir(self)
        self.work = str(Path.home() / "kit-test-project")  # не эфемерный путь; каталог не создаётся
        self.transcript = self.tmp / "projects" / "-kit-test" / "s-1.jsonl"
        self.target = self.tmp / "out" / "-kit-test" / "s-1.md"

    def export(self, records):
        write_jsonl(self.transcript, records)
        return cc.export(self.transcript, self.tmp / "out")

    def test_contract_and_content(self):
        self.assertEqual(self.export(cc_session(self.work)), "written")
        fields = read_frontmatter(self.target)
        for field in REQUIRED_FIELDS:
            self.assertIn(field, fields)
        self.assertEqual(fields["source"], "cc")
        self.assertEqual(fields["truncated"], "false")
        text = self.target.read_text(encoding="utf-8")
        self.assertIn("Почини сборку", text)
        self.assertNotIn("служебное", text)
        self.assertIn("[Bash: make build]", text)
        self.assertIn("[ОТВЕТ ВЛАДЕЛЬЦА] Да", text)
        self.assertIn("варианты: Да; Позже", text)

    def test_secret_is_redacted(self):
        self.export(cc_session(self.work))
        text = self.target.read_text(encoding="utf-8")
        self.assertNotIn(FAKE_KEY, text)
        self.assertIn("[СЕКРЕТ СКРЫТ]", text)

    def test_truncation_is_marked(self):
        self.export(cc_session(self.work, long_answer=True))
        self.assertEqual(read_frontmatter(self.target)["truncated"], "true")
        self.assertIn("[...обрезано, полный текст в исходнике...]", self.target.read_text(encoding="utf-8"))

    def test_ephemeral_session_skipped(self):
        self.assertEqual(self.export(cc_session(os.path.join(tempfile.gettempdir(), "stand"))), "skipped")
        self.assertFalse(self.target.exists())

    def test_fresh_export_not_rewritten(self):
        self.export(cc_session(self.work))
        self.assertEqual(cc.export(self.transcript, self.tmp / "out"), "skipped")

    def test_unreadable_transcript_is_error(self):
        missing = self.tmp / "projects" / "-kit-test" / "missing.jsonl"
        self.assertEqual(cc.export(missing, self.tmp / "out"), "error")


class CodexExporterTest(unittest.TestCase):
    def export(self, extra: list[dict]) -> str:
        tmp = temp_dir(self)
        cwd = str(Path.home() / "kit-test-project")
        records = [
            {"type": "session_meta", "timestamp": "2026-09-28T10:00:00Z", "payload": {"id": "c-1", "cwd": cwd}},
            {"type": "turn_context", "payload": {"cwd": cwd, "model": "gpt-test"}},
            {"type": "response_item", "timestamp": "2026-09-28T10:00:01Z",
             "payload": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": "<environment_context>env</environment_context>"}]}},
            {"type": "event_msg", "timestamp": "2026-09-28T10:00:02Z",
             "payload": {"type": "user_message", "message": "Проверь тесты"}},
            {"type": "response_item", "timestamp": "2026-09-28T10:00:02Z",
             "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Проверь тесты"}]}},
            *extra,
            {"type": "response_item", "timestamp": "2026-09-28T10:00:09Z",
             "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Тесты зелёные."}]}},
        ]
        session_file = write_jsonl(tmp / "rollout-2026-09-28T10-00-00-c-1.jsonl", records)
        self.assertEqual(codex.export(session_file, tmp / "out"), "written")
        target = next((tmp / "out").rglob("*.md"))
        fields = read_frontmatter(target)
        for field in REQUIRED_FIELDS:
            self.assertIn(field, fields)
        return target.read_text(encoding="utf-8")

    def test_duplicates_and_new_tool_format(self):
        text = self.export([
            {"type": "response_item", "timestamp": "2026-09-28T10:00:03Z",
             "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "e1",
                         "input": "tools.exec_command({cmd:'pytest'})"}},
        ])
        # Клиент пишет запрос дважды (событием и сообщением); в выгрузке реплика одна.
        self.assertEqual(text.count("Q: Проверь тесты"), 1)
        self.assertNotIn("environment_context", text)
        self.assertIn('tools: "exec(1)"', text)

    def test_interrupt_and_question(self):
        text = self.export([
            {"type": "event_msg", "timestamp": "2026-09-28T10:00:03Z",
             "payload": {"type": "turn_aborted", "reason": "interrupted"}},
            {"type": "response_item", "timestamp": "2026-09-28T10:00:04Z",
             "payload": {"type": "function_call", "name": "request_user_input", "call_id": "q1",
                         "arguments": json.dumps({"questions": [{"question": "Какой вариант?",
                                                                 "options": [{"label": "A"}, {"label": "B"}]}]})}},
            {"type": "response_item", "timestamp": "2026-09-28T10:00:05Z",
             "payload": {"type": "function_call_output", "call_id": "q1", "output": "B"}},
        ])
        self.assertIn("[ВЛАДЕЛЕЦ ПРЕРВАЛ РАБОТУ]", text)
        self.assertIn("варианты: A; B", text)
        self.assertIn("[ОТВЕТ ВЛАДЕЛЬЦА] B", text)


class ContractTest(unittest.TestCase):
    def test_ephemeral_roots(self):
        self.assertTrue(is_ephemeral_cwd(os.path.join(tempfile.gettempdir(), "x")))
        self.assertFalse(is_ephemeral_cwd(str(Path.home() / "projects" / "app")))
        self.assertFalse(is_ephemeral_cwd(""))

    def test_windows_project_key(self):
        self.assertEqual(project_key_from_cwd(r"C:\Users\me\proj"), "C-Users-me-proj")

    def test_redact(self):
        self.assertEqual(redact(f"ключ {FAKE_KEY} тут"), "ключ [СЕКРЕТ СКРЫТ] тут")
        self.assertEqual(redact("обычный текст"), "обычный текст")


@unittest.skipIf(os.name == "nt", "подставной qmd написан как POSIX-скрипт")
class SyncTest(unittest.TestCase):
    def setUp(self):
        self.home = temp_dir(self)
        self.memory = self.home / "agent-memory"
        self.calls = self.home / "qmd-calls.txt"
        bin_dir = self.home / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "qmd"
        fake.write_text(f"#!/bin/sh\necho \"$@\" >> '{self.calls}'\n", encoding="utf-8")
        fake.chmod(0o755)
        self.env = {**os.environ, "HOME": str(self.home), "USERPROFILE": str(self.home),
                    "AGENT_MEMORY_DIR": str(self.memory), "QMD_BIN": "~/bin/qmd"}

    def run_sync(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(MEMORY / "sync.py"), *args], env=self.env,
                              capture_output=True, text=True)

    def test_full_pass_with_tilde_qmd_bin(self):
        result = self.run_sync("--source", "timer")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls.read_text().split("\n")[:2], ["update", "embed"])
        last = (self.memory / ".sync" / "last-run").read_text()
        self.assertIn("source=timer", last)
        self.assertIn("ok", last)
        self.assertFalse((self.memory / ".sync" / "pending").exists())

    def test_busy_lock_leaves_work_for_holder(self):
        state = self.memory / ".sync"
        state.mkdir(parents=True)
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import fcntl,sys,time; f=open(sys.argv[1],'a+'); fcntl.flock(f, fcntl.LOCK_EX); "
             "print('locked', flush=True); time.sleep(30)", str(state / "sync.lock")],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "locked")
        result = self.run_sync("--source", "hook")
        self.assertEqual(result.returncode, 0)
        self.assertTrue((state / "pending").exists())
        self.assertFalse(self.calls.exists())
        self.assertIn("идёт другой прогон", (state / "sync.log").read_text(encoding="utf-8"))
        holder.kill()
        holder.wait()
        # Замок снят вместе с процессом-держателем: следующий прогон работает без ручной уборки.
        self.assertEqual(self.run_sync("--source", "timer", "--no-embed").returncode, 0)
        self.assertFalse((state / "pending").exists())
        self.assertEqual(self.calls.read_text().split(), ["update"])

    def test_missing_qmd_is_error(self):
        self.env["QMD_BIN"] = str(self.home / "no-such-qmd")
        self.assertEqual(self.run_sync().returncode, 1)
        self.assertIn("error", (self.memory / ".sync" / "last-run").read_text())


if __name__ == "__main__":
    unittest.main()
