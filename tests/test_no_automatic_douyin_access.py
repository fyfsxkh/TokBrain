from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_removed_automation_modules_and_routes_are_absent():
    removed = (
        "app/routers_v2/auth.py",
        "app/routers_v2/sync.py",
        "app/services/accounts.py",
        "app/services/browser_auth.py",
        "app/services/sync.py",
        "app/services/safety_quotas.py",
        "app/adapters/douyin.py",
    )
    assert all(not (ROOT / path).exists() for path in removed)
    main = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert "auth.router" not in main
    assert "sync.router" not in main
    assert "resolve_submitted_link(" not in main


def test_f2_runtime_is_limited_to_single_post_detail():
    source = (ROOT / "app/services/f2_links.py").read_text(encoding="utf-8")
    assert "fetch_post_detail" in source
    assert "PostDetail(" in source
    forbidden = (
        "QueryUser",
        "UserCollects",
        "UserCollection",
        "DouyinHandler",
        "fetch_user_",
        "get_current_user",
        "list_collections",
        "collection_items_page",
    )
    assert all(token not in source for token in forbidden)


def test_frontend_has_no_login_or_remote_collection_actions():
    source = (ROOT / "frontend/app/page.tsx").read_text(encoding="utf-8")
    api = (ROOT / "frontend/lib/api.ts").read_text(encoding="utf-8")
    styles = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "frontend").rglob("*.css")
        if "node_modules" not in path.parts and ".next" not in path.parts
    )
    scripts = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "frontend").rglob("*")
        if path.is_file()
        and path.suffix in {".js", ".mjs", ".ts", ".tsx"}
        and "node_modules" not in path.parts
        and ".next" not in path.parts
        and "tests" not in path.parts
    )
    forbidden = (
        "/api/auth/",
        "startBrowser",
        "captureBrowser",
        "refreshCollections",
        "connectCdp",
        "--remote-debugging-port",
        "自动读取 Cookie",
        "收藏夹刷新",
    )
    assert all(token not in source + api + scripts for token in forbidden)
    assert ".account-chip" not in styles
    assert ".connected-account" not in styles
    assert ".refresh-panel" not in styles


def test_dependencies_have_f2_but_no_browser_or_cdp_stack():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    f2_requirements = (ROOT / "requirements-f2.txt").read_text(
        encoding="utf-8"
    ).lower()
    setup = (ROOT / "scripts" / "setup.ps1").read_text(encoding="utf-8").lower()
    assert "f2==0.0.1.7" not in requirements
    assert "f2==0.0.1.7" in f2_requirements
    assert "requirements-f2.txt" in setup
    assert "--no-deps" in setup
    assert ".vendor" in setup
    assert "playwright" not in requirements
    assert "selenium" not in requirements
