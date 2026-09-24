"""覆盖率补齐用例集：notify_center 全链路 + triage_app 异步/看板/通知端点。"""

# ---------------------------------------------------------------------------
# notify_center.py：notify / load_config / save_config / list_notifications
# ---------------------------------------------------------------------------
def test_notify_file_only_when_no_webhook(tmp_path):
    from notify_center import list_notifications, notify

    r = notify("test-src", "仅落盘通知", base_dir=tmp_path)
    assert r["file"] is True and r["webhook"] == "skipped"
    items = list_notifications(tmp_path, limit=10)
    assert items[-1]["message"] == "仅落盘通知"
    assert items[-1]["source"] == "test-src"
    assert items[-1]["level"] == "info"


def test_notify_config_roundtrip(tmp_path):
    from notify_center import load_config, save_config

    cfg = {"webhook_url": "http://127.0.0.1:9/hook", "enabled": True}
    save_config(tmp_path, cfg)
    # M3.6：webhook_type 为缺省合并字段——不写则补默认 generic（向后兼容语义）
    loaded = load_config(tmp_path)
    assert loaded["webhook_url"] == cfg["webhook_url"]
    assert loaded["enabled"] is True
    assert loaded["webhook_type"] == "generic"


def test_notify_config_corrupt_falls_back(tmp_path):
    from notify_center import load_config

    (tmp_path / "notify_config.json").write_text("{broken", encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg["enabled"] is False and cfg["webhook_url"] == ""


def test_notify_webhook_success(tmp_path, monkeypatch):
    """webhook 可达 → sent（mock urlopen 避免真实出网）。"""
    import notify_center as nc

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["data"] = req.data
        return FakeResp()

    monkeypatch.setattr(nc.urllib.request, "urlopen", fake_urlopen)
    nc.save_config(tmp_path, {"webhook_url": "http://hook.local/x", "enabled": True})
    r = nc.notify("src", "推给webhook", base_dir=tmp_path)
    assert r["webhook"] == "sent" and r["file"] is True
    assert captured["url"] == "http://hook.local/x"


def test_notify_webhook_timeout_silent(tmp_path, monkeypatch):
    """webhook 超时 → failed 静默、文件不丢（D-013 教训：通知绝不阻断业务）。"""
    import notify_center as nc

    def boom(req, timeout):
        raise TimeoutError("timed out")

    monkeypatch.setattr(nc.urllib.request, "urlopen", boom)
    nc.save_config(tmp_path, {"webhook_url": "http://slow.local/x", "enabled": True})
    r = nc.notify("src", "超时演练", base_dir=tmp_path)
    assert r["webhook"] == "failed" and r["file"] is True


def test_notify_ledger_bad_line_skipped(tmp_path):
    from notify_center import list_notifications

    (tmp_path / "notifications.jsonl").write_text(
        '{"ts":"t","source":"s","message":"ok"}\n{bad line\n', encoding="utf-8"
    )
    items = list_notifications(tmp_path, limit=10)
    assert len(items) == 1 and items[0]["message"] == "ok"


