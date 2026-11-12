import asyncio
import sys

from rustic_scheduler.redact import REDACTOR, RedactingFilter, Redactor
from rustic_scheduler.runner import run_process

PY = sys.executable


async def run(code, **kwargs):
    defaults = {"timeout": 10, "stop": asyncio.Event(), "stop_grace": 1}
    return await run_process([PY, "-c", code], {}, **(defaults | kwargs))


async def test_ndjson_messages_and_plain_lines_are_separated(tmp_path):
    code = (
        'import sys;print(\'{"message_type":"status","bytes_done":1}\');print(\'plain text\');'
        "print('[1,2]');print('{\"message_type\":\"summary\"}');sys.stderr.write('[WARN] careful\\n')"
    )
    seen = []
    log_file = tmp_path / "logs" / "run.log"
    result = await run(code, on_message=seen.append, log_file=log_file)
    assert result.exit_code == 0
    assert (
        [m["message_type"] for m in result.messages]
        == ["status", "summary"]
        == [m["message_type"] for m in seen]
    )
    assert result.stderr_tail == ["[WARN] careful"]
    text = log_file.read_text()
    assert "plain text" in text and "[1,2]" in text and "summary" in text and "careful" in text
    assert "bytes_done" not in text  # progress chatter stays out of the run log
    assert oct(log_file.stat().st_mode & 0o777) == "0o600"


async def test_very_long_lines_do_not_break_parsing():
    code = "import json;print(json.dumps({'message_type':'summary','pad':'x'*300000}))"
    result = await run(code)
    assert len(result.messages) == 1 and len(result.messages[0]["pad"]) == 300000


async def test_capture_mode_returns_raw_stdout():
    result = await run("print('[{\"a\": 1}]')", mode="capture")
    assert result.stdout.strip() == b'[{"a": 1}]' and result.messages == []


async def test_a_failing_progress_handler_does_not_fail_the_process():
    def boom(_):
        raise RuntimeError("reporting bug")

    result = await run('print(\'{"message_type":"status"}\')', on_message=boom)
    assert result.exit_code == 0 and len(result.messages) == 1


async def test_timeout_terminates_then_reports():
    result = await run("import time;time.sleep(30)", timeout=0.3)
    assert result.timed_out and not result.stopped_by_scheduler and result.exit_code == -15


async def test_stop_event_terminates_and_escalates_to_kill():
    stop = asyncio.Event()
    code = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print('up',flush=True);time.sleep(30)"
    task = asyncio.create_task(run(code, stop=stop, stop_grace=0.3, mode="capture"))
    await asyncio.sleep(0.4)
    stop.set()
    result = await asyncio.wait_for(task, 10)
    assert result.stopped_by_scheduler and result.exit_code == -9


async def test_interrupt_marker_is_detected():
    code = "import sys;sys.stderr.write('[INFO] Ctrl-C received, shutting down...\\n')"
    assert (await run(code)).saw_interrupt


async def test_non_terminal_error_logs_actionable_hint_once(tmp_path, caplog):
    log_file = tmp_path / "run.log"
    code = "import sys;sys.stderr.write('error: IO error: not a terminal\\n' * 2);sys.exit(1)"
    result = await run(code, mode="capture", log_file=log_file, name="rustic[docs]")
    assert result.exit_code == 1
    assert "error: IO error: not a terminal" in result.tail
    for text in (caplog.text, log_file.read_text(), result.tail):
        assert text.count("password-file") == 1
        assert "password-command" in text
        assert "hook" in text
    assert "rustic[docs]" in caplog.text


async def test_other_errors_do_not_log_non_terminal_hint(caplog):
    result = await run("import sys;sys.stderr.write('error: permission denied\\n');sys.exit(1)")
    assert result.exit_code == 1
    assert "password-file" not in result.tail
    assert "password-file" not in caplog.text


async def test_child_processes_are_stopped_with_the_group():
    code = (
        "import subprocess,sys,time;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        "print(p.pid,flush=True);time.sleep(60)"
    )
    result = await run(code, timeout=0.5, mode="capture")
    child = int(result.stdout.split()[0])
    await asyncio.sleep(0.2)
    import os

    try:
        os.kill(child, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    assert not alive


async def test_spawn_error_is_reported_not_raised(tmp_path):
    result = await run_process(
        ["/nonexistent/rustic"],
        {},
        timeout=1,
        stop=asyncio.Event(),
        stop_grace=1,
        log_file=tmp_path / "x.log",
    )
    assert result.spawn_error and result.exit_code is None
    assert "could not start" in (tmp_path / "x.log").read_text()


async def test_output_is_redacted_in_the_run_log(tmp_path):
    REDACTOR.register(["s3cr3t-value"])
    code = (
        "import sys;sys.stderr.write('connecting to mqtts://u:hunter2@broker with s3cr3t-value\\n')"
    )
    log_file = tmp_path / "run.log"
    result = await run(code, log_file=log_file)
    for text in (log_file.read_text(), result.tail):
        assert "hunter2" not in text and "s3cr3t-value" not in text and "[REDACTED]" in text


def test_redactor_patterns():
    redact = Redactor()
    redact.register(["tok_abcdef123", None, "", "ab"])
    assert redact("x tok_abcdef123 y") == "x [REDACTED] y"
    assert redact("ab") == "ab"  # too short to be a meaningful secret
    assert "hunter2" not in redact("https://user:hunter2@example.com/path")
    assert redact("https://example.com/path") == "https://example.com/path"
    assert "abc" not in redact("secret_access_key = abc")
    assert "abc" not in redact('RUSTIC_PASSWORD="abc"')
    assert "AKIAIOSFODNN7EXAMPLE" not in redact("key AKIAIOSFODNN7EXAMPLE used")
    assert redact("password-file = /run/secrets/x") == "password-file = [REDACTED]"


def test_logging_filter_redacts_arguments(caplog):
    import logging

    REDACTOR.register(["apprise-token-123456"])
    logger = logging.getLogger("redaction-test")
    logger.addFilter(RedactingFilter())
    with caplog.at_level(logging.INFO, logger="redaction-test"):
        logger.info("sending to %s", "tgram://apprise-token-123456/chat")
    assert "apprise-token-123456" not in caplog.text and "[REDACTED]" in caplog.text
