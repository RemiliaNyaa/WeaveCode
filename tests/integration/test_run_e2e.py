"""
End-to-end integration test for the S1 agent pipeline.

Requires a real ANTHROPIC_API_KEY — skipped automatically when absent.
Run explicitly:
    uv run pytest tests/integration/test_run_e2e.py -v
Or with the marker:
    uv run pytest -m integration -v
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from dotenv import load_dotenv

from weavecode.core.config import WeaveConfig
from weavecode.core.events.writer import read_events
from weavecode.core.runner import AgentRunner
from weavecode.core.storage import Database, apply_migrations

# Load project .env so ANTHROPIC_API_KEY is available without going through get_config()
load_dotenv(Path(__file__).parent.parent.parent / ".env", override=False)

pytestmark = pytest.mark.integration


@pytest.fixture()
def sample_file(tmp_path: Path) -> Path:
    f = tmp_path / "sample.txt"
    f.write_text(
        "# Test Document\n\nThe magic number mentioned in this file is 7391.\n",
        encoding="utf-8",
    )
    return f


# 功能：验证完整的端到端 agent 链路：调用真实 LLM → 执行 read_file → 成功完成并把事件落进 event 表
# 设计：使用真实 ANTHROPIC_API_KEY 和真实文件，goal 中指定一个具体的数字（7391）以便断言 LLM 确实读了文件；
#       通过 event 表里的事件序列断言每个关键阶段都被记录，而非只检查 stdout，因为事件流是 S1 的核心验收产物
async def test_run_e2e_reads_file_and_succeeds(
    sample_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set")

    # ReadFileTool resolves paths relative to CWD — point it at tmp_path
    monkeypatch.chdir(tmp_path)

    goal = (
        "Use the read_file tool to read the file 'sample.txt' "
        "and report the magic number it mentions."
    )

    config = WeaveConfig()
    config.agent.max_steps = 5

    db = Database(tmp_path / "e2e.db")
    await apply_migrations(db)

    runner = AgentRunner(config, db=db)
    await runner.run(goal)

    # ── exactly one run must be persisted ────────────────────────────────────
    def _run_ids(conn: sqlite3.Connection) -> list[str]:
        rows = conn.execute("SELECT DISTINCT run_id FROM event").fetchall()
        return [str(r["run_id"]) for r in rows]

    run_ids = await db.run(_run_ids)
    assert len(run_ids) == 1, "expected exactly one run in the event table"

    events = await read_events(db, run_ids[0])
    types = [e["type"] for e in events]

    # ── event sequence assertions (from §6.4) ────────────────────────────────
    assert types[0] == "run.started"
    assert types[-1] == "run.finished"
    assert "step.started" in types
    assert "tool.call_started" in types
    assert "tool.call_finished" in types
    assert "llm.usage" in types

    # ── run completed successfully ────────────────────────────────────────────
    finished = events[-1]
    assert finished["status"] == "success", (
        f"run finished with status={finished['status']!r}, reason={finished.get('reason')!r}"
    )

    # ── read_file was actually invoked ────────────────────────────────────────
    tool_starts = [e for e in events if e["type"] == "tool.call_started"]
    assert any(e["tool_name"] == "read_file" for e in tool_starts), (
        "expected at least one read_file tool call"
    )

    # ── run_id is consistent across the event stream ─────────────────────────
    run_id = events[0]["run_id"]
    assert all(e["run_id"] == run_id for e in events), "run_id must be the same in every event"

    # ── LLM cache stats are present ──────────────────────────────────────────
    usage_events = [e for e in events if e["type"] == "llm.usage"]
    assert len(usage_events) >= 1
    for ue in usage_events:
        assert "input_tokens" in ue
        assert "output_tokens" in ue
        assert "cache_read_input_tokens" in ue
