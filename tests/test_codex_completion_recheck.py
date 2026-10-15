"""Native journal verification is bounded, read-only and exact-session scoped."""
import json
import asyncio
from datetime import datetime, timezone

import pytest

import codex_client


@pytest.fixture
def journal(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    when = datetime(2026, 1, 2, tzinfo=timezone.utc)
    stamp = f"{int(when.timestamp() * 1000):012x}"
    session = f"{stamp[:8]}-{stamp[8:]}-7000-8000-000000000001"
    folder = tmp_path / "sessions" / "2026" / "01" / "02"
    folder.mkdir(parents=True)
    path = folder / f"rollout-fixture-{session}.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": session}},
        {"type": "event_msg", "timestamp": "2026-01-02T00:00:02Z",
         "payload": {"type": "task_complete", "turn_id": "fixture-turn", "last_agent_message": "Finished.",
                     "started_at": when.timestamp()}},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return session, path


async def test_exact_completed_journal_needs_no_provider(journal, monkeypatch):
    session, path = journal
    def forbidden(*args, **kwargs):
        raise AssertionError("verification must not launch a provider")
    monkeypatch.setattr(codex_client, "_find_codex_exe", forbidden)
    before = path.read_bytes()
    record = await codex_client.read_codex_last_turn(session, str(path.parent))
    assert record["status"] == "completed"
    assert record["id"] == "fixture-turn"
    assert record["completedAt"] == datetime(2026, 1, 2, 0, 0, 2, tzinfo=timezone.utc).timestamp()
    assert path.read_bytes() == before
    assert "payload" not in record and "text" not in record


@pytest.mark.parametrize("suffix", [
    json.dumps({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "new-turn"}}) + "\n",
    json.dumps({"type": "event_msg", "payload": {"type": "turn_aborted"}}) + "\n",
    '{"type":"event_msg",',
])
async def test_newer_started_aborted_or_incomplete_record_blocks_recovery(journal, suffix):
    session, path = journal
    path.write_text(path.read_text(encoding="utf-8") + suffix, encoding="utf-8")
    assert await codex_client.read_codex_last_turn(session, str(path.parent)) is None


async def test_missing_duplicate_or_wrong_session_header_blocks_recovery(journal):
    session, path = journal
    assert await codex_client.read_codex_last_turn("not-a-native-uuid", str(path.parent)) is None
    path.with_name(f"rollout-duplicate-{session}.jsonl").write_bytes(path.read_bytes())
    assert await codex_client.read_codex_last_turn(session, str(path.parent)) is None
    assert codex_client._completed_rollout_tail(str(path), "different-session") is None


@pytest.mark.parametrize("update", [{"error": {"message": "usage limit"}}, {"last_agent_message": None}])
async def test_task_complete_is_not_itself_proof_of_success(journal, update):
    session, path = journal
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    records[-1]["payload"].update(update)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    assert await codex_client.read_codex_last_turn(session, str(path.parent)) is None


@pytest.mark.parametrize('witness', [None, {'id':'old-turn','status':'completed'},
    {'id':'active-turn','status':'running'}, {'id':'active-turn','status':'completed','error':'failed'},
    {'id':'active-turn','status':'completed','error':None}])
async def test_lost_terminal_recovers_only_exact_successful_native_turn(tmp_path, monkeypatch, witness):
    checks = []
    class Input:
        closed = False
        def is_closing(self): return self.closed
        def close(self): self.closed = True
        def write(self, line):
            request = json.loads(line)
            method = request.get('method')
            if request.get('id') is None: return
            result = {}
            if method == 'thread/start':
                result = {'thread':{'id':'fixture-session'},'model':'gpt-6.1-sol','modelProvider':'openai'}
            elif method == 'turn/start': result = {'turn':{'id':'active-turn'}}
            proc.feed({'id':request['id'],'result':result})
            if method == 'turn/start':
                proc.feed({'method':'item/agentMessage/delta','params':{
                    'threadId':'fixture-session','turnId':'active-turn','delta':'Finished reply.'}})
                # Long-lived thread idle is not completion authority.
                proc.feed({'method':'thread/status/changed','params':{
                    'threadId':'fixture-session','status':{'type':'idle'}}})
        async def drain(self): await asyncio.sleep(0)
    class Process:
        pid = 987654321
        returncode = None
        def __init__(self):
            self.stdin = Input()
            self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        def feed(self, event): self.stdout.feed_data((json.dumps(event)+'\n').encode())
        def terminate(self):
            self.returncode = 0
            self.stdout.feed_eof(); self.stderr.feed_eof()
        kill = terminate
        async def wait(self): return self.returncode
    proc = Process()
    async def spawn(*args, **kwargs): return proc
    async def verify(session, cwd):
        checks.append((session, cwd, proc.returncode))
        if witness is None or witness.get('id') != 'active-turn' or witness.get('status') != 'completed' or witness.get('error'):
            # Allow normal protocol completion only after the negative probe.
            proc.feed({'method':'turn/completed','params':{
                'threadId':'fixture-session','turn':{'id':'active-turn','status':'completed','items':[]}}})
        return witness
    monkeypatch.setattr(codex_client.asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(codex_client, '_find_codex_exe', lambda: 'fixture-codex')
    monkeypatch.setattr(codex_client, '_thread_mcp_servers', lambda *a, **k: {})
    monkeypatch.setattr(codex_client.sys, 'platform', 'linux')
    monkeypatch.setattr(codex_client, 'read_codex_last_turn', verify)
    monkeypatch.setattr(codex_client, '_CODEX_COMPLETION_RECHECK_SEC', .01)
    _, stream = await codex_client.run_codex('fixture', str(tmp_path), model='gpt-6.1-sol')
    async with asyncio.timeout(2): events = [event async for event in stream]
    assert checks == [('fixture-session', str(tmp_path), None)]
    result = next(event for event in events if event['type']=='result')
    assert result['terminal_confirmed'] and not result['is_error']
    assert result['result_text']=='Finished reply.'
    recovered = any('recovering its missing completion' in event.get('text','') for event in events)
    assert recovered == bool(witness and witness.get('id')=='active-turn' and witness.get('status')=='completed' and not witness.get('error'))
    assert proc.returncode == 0
