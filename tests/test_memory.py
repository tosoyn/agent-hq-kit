"""Тесты выгрузки истории на синтетических сессиях. Запуск: python3 -m unittest discover tests

Живые данные не трогаются: сессии собираются во временном каталоге, выгрузка пишется
туда же. Нужен только Python 3.10+.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MEMORY = Path(__file__).resolve().parents[1] / "memory"
sys.path.insert(0, str(MEMORY))

import cc_history_to_md as cc  # noqa: E402
import codex_history_to_md as codex  # noqa: E402
from contract import REQUIRED_FIELDS, is_ephemeral_cwd, read_frontmatter  # noqa: E402


def write_jsonl(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
    return path


def cc_session(cwd: str, long_answer: bool = False) -> list[dict]:
    base = {"sessionId": "s-1", "cwd": cwd, "gitBranch": "main"}
    answer = "Готово: поправил импорт." + (" x" * 20000 if long_answer else "")
    return [
        {**base, "type": "user", "timestamp": "2026-09-28T10:00:00Z",
         "message": {"role": "user", "content": "Почини сборку <system-reminder>служебное</system-reminder>"}},
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
        self.tmp = Path(tempfile.mkdtemp())
        self.work = Path.home() / "kit-test-project"  # не эфемерный путь; сам каталог не создаётся

    def export(self, records):
        transcript = write_jsonl(self.tmp / "projects" / "-kit-test" / "s-1.jsonl", records)
        out = self.tmp / "out"
        written = cc.export(transcript, out)
        target = out / "-kit-test" / "s-1.md"
        return written, target

    def test_contract_and_content(self):
        written, target = self.export(cc_session(str(self.work)))
        self.assertTrue(written)
        fields = read_frontmatter(target)
        for field in REQUIRED_FIELDS:
            self.assertIn(field, fields)
        self.assertEqual(fields["source"], "cc")
        self.assertEqual(fields["truncated"], "false")
        text = target.read_text(encoding="utf-8")
        self.assertIn("Почини сборку", text)
        self.assertNotIn("служебное", text)
        self.assertIn("[Bash: make build]", text)
        self.assertIn("[ОТВЕТ ВЛАДЕЛЬЦА] Да", text)
        self.assertIn("варианты: Да; Позже", text)

    def test_truncation_is_marked(self):
        _, target = self.export(cc_session(str(self.work), long_answer=True))
        self.assertEqual(read_frontmatter(target)["truncated"], "true")
        self.assertIn("[...обрезано, полный текст в исходнике...]", target.read_text(encoding="utf-8"))

    def test_ephemeral_session_skipped(self):
        written, target = self.export(cc_session(tempfile.gettempdir() + "/stand"))
        self.assertFalse(written)
        self.assertFalse(target.exists())

    def test_fresh_export_not_rewritten(self):
        self.export(cc_session(str(self.work)))
        written, _ = self.export(cc_session(str(self.work)))
        # write_jsonl обновил исходник, значит выгрузка старше и переписывается
        self.assertTrue(written)
        transcript = self.tmp / "projects" / "-kit-test" / "s-1.jsonl"
        self.assertFalse(cc.export(transcript, self.tmp / "out"))


class CodexExporterTest(unittest.TestCase):
    def test_duplicates_and_new_tool_format(self):
        tmp = Path(tempfile.mkdtemp())
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
            {"type": "response_item", "timestamp": "2026-09-28T10:00:03Z",
             "payload": {"type": "custom_tool_call", "name": "exec", "input": "tools.exec_command({cmd:'pytest'})"}},
            {"type": "response_item", "timestamp": "2026-09-28T10:00:04Z",
             "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Тесты зелёные."}]}},
        ]
        session_file = write_jsonl(tmp / "rollout-2026-09-28T10-00-00-c-1.jsonl", records)
        self.assertTrue(codex.export(session_file, tmp / "out"))
        target = next((tmp / "out").rglob("*.md"))
        fields = read_frontmatter(target)
        for field in REQUIRED_FIELDS:
            self.assertIn(field, fields)
        text = target.read_text(encoding="utf-8")
        # Клиент пишет запрос дважды (событием и сообщением); в выгрузке реплика одна.
        self.assertEqual(text.count("Q: Проверь тесты"), 1)
        self.assertNotIn("environment_context", text)
        self.assertIn('tools: "exec(1)"', text)


class ContractTest(unittest.TestCase):
    def test_ephemeral_roots(self):
        self.assertTrue(is_ephemeral_cwd(os.path.join(tempfile.gettempdir(), "x")))
        self.assertFalse(is_ephemeral_cwd(str(Path.home() / "projects" / "app")))
        self.assertFalse(is_ephemeral_cwd(""))


class SyncLockTest(unittest.TestCase):
    def test_second_run_leaves_pending_mark(self):
        tmp = Path(tempfile.mkdtemp())
        env = {**os.environ, "AGENT_MEMORY_DIR": str(tmp), "SYNC_EXPORTERS": "",
               "QMD_BIN": str(tmp / "no-qmd")}
        state = tmp / ".sync"
        state.mkdir(parents=True)
        (state / "sync.lock").write_text("1 0\n")  # свежий чужой замок
        result = subprocess.run([sys.executable, str(MEMORY / "sync.py"), "--no-embed"], env=env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertTrue((state / "pending").exists())


if __name__ == "__main__":
    unittest.main()
