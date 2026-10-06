"""Generalization: nothing is tied to one person's machine. Data dir, labels, read-only accounts, work item
pattern, optional plugins, launchd prefix, and "absence is never green" when no usage source exists."""
import json
import os
import stat
import sys

import pytest

from dhi_orbit import collector, config, panels, server
from dhi_orbit.cp import db, sources, work, workitems


@pytest.fixture
def conf(tmp_path, monkeypatch):
    """Write config.json (the data dir's settings file) and return a function to set its content."""
    p = tmp_path / "orbit-home" / "config.json"
    p.parent.mkdir(parents=True, exist_ok=True)

    def put(**kw):
        p.write_text(json.dumps(kw))
    put()
    return put


def test_data_dir_defaults_to_dot_config_and_env_overrides(monkeypatch, tmp_path):
    monkeypatch.delenv("DHI_ORBIT_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "fresh"))                       # no ~/.config/* at all
    assert config.home() == str(tmp_path / "fresh" / ".config" / "dhi-orbit")
    monkeypatch.setenv("DHI_ORBIT_HOME", str(tmp_path / "x"))
    assert config.home() == str(tmp_path / "x")
    assert config.db_path() == str(tmp_path / "x" / "orbit.db")


def test_legacy_home_dir_is_used_until_a_new_one_exists(monkeypatch, tmp_path):
    monkeypatch.delenv("DHI_ORBIT_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    old, new = tmp_path / ".config" / "chatdash", tmp_path / ".config" / "dhi-orbit"
    old.mkdir(parents=True)
    assert config.home() == str(old)                                          # a chatdash install that has not moved
    new.mkdir()
    assert config.home() == str(new)                                          # the new dir wins once it exists


def test_legacy_chatdash_env_vars_are_still_honoured_and_the_new_name_wins(monkeypatch, tmp_path):
    monkeypatch.delenv("DHI_ORBIT_HOME", raising=False)
    monkeypatch.setenv("CHATDASH_HOME", str(tmp_path / "old"))
    assert config.home() == str(tmp_path / "old")
    monkeypatch.setenv("CHATDASH_CONFIG", str(tmp_path / "old.json"))
    assert config.path() == str(tmp_path / "old.json")
    monkeypatch.setenv("DHI_ORBIT_HOME", str(tmp_path / "new"))
    monkeypatch.setenv("DHI_ORBIT_CONFIG", str(tmp_path / "new.json"))
    assert config.home() == str(tmp_path / "new") and config.path() == str(tmp_path / "new.json")
    assert config.env("NOPE", "dflt") == "dflt" and config.env("NOPE", environ={"CHATDASH_NOPE": "v"}) == "v"


def test_legacy_database_file_is_kept_until_a_new_one_exists(tmp_path):
    h = tmp_path / "orbit-home"
    h.mkdir()
    assert config.db_path() == str(h / "orbit.db")                            # fresh install
    (h / "chatdash.db").write_text("")
    assert config.db_path() == str(h / "chatdash.db")                         # existing chatdash data
    (h / "orbit.db").write_text("")
    assert config.db_path() == str(h / "orbit.db")                            # the new name wins


def test_token_is_created_on_first_run_with_mode_600_in_the_data_dir(tmp_path):
    assert not os.path.exists(config.home())
    tok = server.load_token()
    assert len(tok) >= 24 and server.load_token() == tok                      # stable across runs
    assert stat.S_IMODE(os.stat(config.token_path()).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(config.home()).st_mode) == 0o700
    assert config.token_path().startswith(str(tmp_path))                     # never next to the code


def test_work_items_off_by_default_and_pattern_configurable(conf):
    assert config.work_item_of("PROJ-12 Repos") is None and collector.workstream_of("PROJ-12 Repos") is None
    conf(work_item_pattern=r"PROJ-\d+")
    assert config.work_item_of("PROJ-12 Repos") == "PROJ-12" and config.work_item_of("plain chat") is None
    conf(work_item_pattern="(")                                              # a broken pattern never raises
    assert config.work_item_of("PROJ-12") is None


def test_labels_derive_from_the_dir_name_and_can_be_overridden(conf):
    assert config.account_label("work") == "Work" and config.account_label("my-team") == "My team"
    assert config.account_of_config("/h/.claude") == "main" and config.account_of_config("/h/.claude-work") == "work"
    conf(seat_labels={"work": "Work laptop"})
    assert config.account_label("work") == "Work laptop" and config.account_label("other") == "Other"


def test_no_account_is_read_only_unless_configured(conf):
    assert not config.is_read_only("main") and not config.is_read_only("work")
    assert config.is_read_only(None) and config.is_read_only(cfg=None)         # nothing to act on
    conf(read_only_accounts=["main"])
    assert config.is_read_only("main") and config.is_read_only(cfg="/h/.claude") and not config.is_read_only(cfg="/h/.claude-work")


def test_best_seat_falls_back_to_the_first_discovered_account(conf, monkeypatch):
    monkeypatch.setattr(collector, "config_dirs", lambda: ["/h/.claude", "/h/.claude-work"])
    unknown = {"main": {"five": None, "seven": None, "ok": False}, "work": {"five": None, "seven": None, "ok": False}}
    assert server.best_seat(unknown) == "main"
    conf(read_only_accounts=["main"])
    assert server.best_seat(unknown) == "work"                                 # a read-only account is never picked
    assert server.best_seat({"main": {"five": 1, "seven": 1, "ok": True}, "work": {"five": 50, "seven": 50, "ok": True}}) == "work"
    monkeypatch.setattr(collector, "config_dirs", lambda: [])
    assert server.best_seat({}) == ""


def test_default_cwd_is_home_or_config(conf):
    assert config.default_cwd() == os.path.expanduser("~")
    conf(default_cwd="~/code")
    assert config.default_cwd() == os.path.expanduser("~/code")


def test_public_host_is_empty_by_default_and_comes_from_config(conf):
    assert config.public_url() == "" and server.load_public() == {}
    conf(public_url="https://board.example.org/")
    assert server.load_public()["host"] == "board.example.org"


def test_plugin_absent_means_none_and_present_loads(conf, tmp_path):
    assert config.plugin("usage_live") is None
    conf(plugins={"usage_live": {"path": str(tmp_path / "nope")}})
    assert config.plugin("usage_live") is None                                 # configured but the folder is missing
    d = tmp_path / "plug"
    d.mkdir()
    (d / "my_usage.py").write_text("def fetch_live(name):\n    return {'five': 11, 'seven': 22}\n")
    conf(plugins={"usage_live": {"path": str(d), "module": "my_usage"}})
    try:
        assert config.plugin("usage_live").fetch_live("work") == {"five": 11, "seven": 22}
    finally:
        sys.modules.pop("my_usage", None)
        sys.path.remove(str(d))


def test_no_usage_source_means_unknown_never_green(conf, monkeypatch, tmp_path):
    monkeypatch.setattr(collector, "config_dirs", lambda: [str(tmp_path / ".claude-work")])
    col = collector.Collector()
    seat = col.seat_usage()["work"]
    assert seat["ok"] is False and seat["five"] is None and seat["seven"] is None
    src = sources.Sources(col=col)
    monkeypatch.setattr(sources.collector, "config_dirs", lambda: [str(tmp_path / ".claude-work")])
    snap = src.refresh(chats=[])
    st = snap["seats"][0]
    assert st["state"] == "unknown" and st["label"] == "Work" and "meter" in snap["errors"]


def test_live_reading_comes_only_from_the_optional_plugin(conf, monkeypatch, tmp_path):
    d = tmp_path / "plug"
    d.mkdir()
    (d / "ul_mod.py").write_text("def fetch_live(name):\n    return {'five': 30, 'seven': 40}\n")
    conf(plugins={"usage_live": {"path": str(d), "module": "ul_mod"}})
    monkeypatch.setattr(collector, "config_dirs", lambda: [str(tmp_path / ".claude-work")])
    try:
        seat = collector.Collector().seat_usage()["work"]
    finally:
        sys.modules.pop("ul_mod", None)
        sys.path.remove(str(d))
    assert seat["ok"] is True and (seat["five"], seat["seven"]) == (30, 40)


def test_launchd_prefix_is_configurable_and_the_dashboard_job_is_protected(conf):
    assert config.launchd_prefix() == "com.dhi.orbit"
    assert panels.is_protected("com.dhi.orbit") and panels.is_protected("com.dhi.orbit.server")
    assert panels.is_protected("com.dhi.orbit.tunnel") and not panels.is_protected("com.dhi.orbit.backup")
    assert panels.schedule_toggle("com.other.job", True)["ok"] is False
    assert "dashboard itself" in panels.schedule_toggle("com.dhi.orbit.server", False)["error"]
    conf(launchd_prefix="org.example.board")
    assert config.launchd_prefix() == "org.example.board"
    assert panels.is_protected("org.example.board.server") and not panels.is_protected("org.example.board.sync")
    assert panels.schedule_toggle("com.dhi.orbit.backup", True)["ok"] is False  # the old prefix no longer matches
    conf(launchd_prefix="bad prefix; rm -rf")
    assert config.launchd_prefix() == "com.dhi.orbit"


def test_orbit_and_legacy_chatdash_job_names_are_protected_under_a_custom_prefix(conf):
    conf(launchd_prefix="com.example")
    assert panels.is_protected("com.example.orbit") and panels.is_protected("com.example.chatdash")
    assert not panels.is_protected("com.example.backup")


def test_work_items_are_seeded_from_matching_chats_only(conf, tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", str(tmp_path / "w.db"))
    db.init()
    chats = [{"ws": "PROJ-1"}, {"ws": "PROJ-1"}, {"ws": None}, {}]
    assert workitems.seed(chats) == 1 and workitems.seed(chats) == 0           # idempotent
    row = db.rows("SELECT id, title FROM work_items")
    assert row == [{"id": "PROJ-1", "title": "PROJ-1"}]
    assert workitems.seed([]) == 0 and workitems.seed(None) == 0


def test_a_work_item_without_a_pattern_never_exists(tmp_path, monkeypatch):
    chat = {"name": "PROJ-1 thing"}
    assert collector.workstream_of(chat["name"]) is None                       # default: everything groups by seat


def test_state_docs_live_in_the_data_dir_by_default(tmp_path):
    assert work.state_path("PROJ-1").startswith(str(tmp_path / "orbit-home" / "work" / "state"))


def test_clock_uses_the_configured_zone_else_local(conf):
    from dhi_orbit.cp import limits
    at = 1_790_000_000.0
    conf(timezone="UTC")
    utc = limits.fmt_clock(at)
    conf(timezone="Asia/Tokyo")
    assert limits.fmt_clock(at) != utc
    conf()
    assert limits.fmt_clock(at)                                                # local zone: some clock string
    assert limits.fmt_clock(None) is None


def test_login_page_and_cli_help_name_no_private_host(capsys):
    assert "dhi-tech" not in server.LOGIN_PAGE.lower() and "DHI Orbit" in server.LOGIN_PAGE
    with pytest.raises(SystemExit) as e:
        sys.argv = ["dhi-orbit", "--help"]
        server.main()
    assert e.value.code == 0
    assert "usage: dhi-orbit" in capsys.readouterr().out


def test_static_page_takes_the_public_url_from_config_not_from_the_source():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    page = open(os.path.join(here, "dhi_orbit", "static", "index.html"), encoding="utf-8").read()
    assert 'const PUBLIC_URL="__PUBLIC_URL__"' in page and 'value="https://' not in page


def test_launchd_prefix_follows_legacy_home(tmp_path, monkeypatch):
    monkeypatch.setenv("DHI_ORBIT_HOME", str(tmp_path / "chatdash"))
    assert config.launchd_prefix() == "com.chatdash"
    monkeypatch.setenv("DHI_ORBIT_HOME", str(tmp_path / "dhi-orbit"))
    assert config.launchd_prefix() == "com.dhi.orbit"
