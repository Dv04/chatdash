"""contrib/statusline-meter.py writes the log DHI Orbit's Meter reads, and only when the numbers change."""
import importlib.util
import io
import json
import os

from dhi_orbit.cp import sources

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("statusline_meter", os.path.join(HERE, "contrib", "statusline-meter.py"))
meter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(meter)

RL = {"five_hour": {"used_percentage": 42, "resets_at": 2_000_000_000}, "seven_day": {"used_percentage": 7, "resets_at": 2_000_500_000}}


def run(tmp_path, doc, cfg="/h/.claude-work"):
    env = {"DHI_ORBIT_HOME": str(tmp_path / "home"), "CLAUDE_CONFIG_DIR": cfg}
    return meter.main(stdin=io.StringIO(json.dumps(doc)), env=env)


def test_logs_a_reading_that_the_meter_reads_back_and_skips_repeats(tmp_path):
    assert run(tmp_path, {"session_id": "s1", "rate_limits": RL}) == 0
    assert run(tmp_path, {"session_id": "s1", "rate_limits": RL}) == 0                      # unchanged: no new line
    log = tmp_path / "home" / "meter.log"
    assert len(log.read_text().splitlines()) == 1
    m = sources.Meter(str(log)).read()["/h/.claude-work"]
    assert (m["five"], m["seven"], m["five_resets"]) == (42, 7, 2_000_000_000)
    RL2 = {"five_hour": {"used_percentage": 43, "resets_at": 2_000_000_000}, "seven_day": RL["seven_day"]}
    run(tmp_path, {"session_id": "s1", "rate_limits": RL2})
    assert len(log.read_text().splitlines()) == 2


def test_no_rate_limits_or_bad_input_writes_nothing_and_never_raises(tmp_path):
    assert run(tmp_path, {"session_id": "s1"}) == 0
    assert meter.main(stdin=io.StringIO("not json"), env={"DHI_ORBIT_HOME": str(tmp_path / "home")}) == 0
    assert not (tmp_path / "home" / "meter.log").exists()


def test_legacy_chatdash_home_env_and_data_dir_are_still_honoured(tmp_path):
    doc = {"session_id": "s1", "rate_limits": RL}
    env = {"CHATDASH_HOME": str(tmp_path / "old"), "CLAUDE_CONFIG_DIR": "/h/.claude-work"}
    assert meter.main(stdin=io.StringIO(json.dumps(doc)), env=env) == 0
    assert (tmp_path / "old" / "meter.log").exists()
    env = dict(env, DHI_ORBIT_HOME=str(tmp_path / "new"))                                    # the new name wins
    assert meter.main(stdin=io.StringIO(json.dumps(doc)), env=env) == 0
    assert (tmp_path / "new" / "meter.log").exists()
