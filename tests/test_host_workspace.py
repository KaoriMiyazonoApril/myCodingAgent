from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from agent.host.app import create_app
from agent.host.model_catalog import ModelDiscovery
from agent.host.provider_config import ProviderStore
from agent.host.workspace import WorkspaceBrowser


class _Catalog:
    async def discover(self, provider_id: str, api_key: str) -> ModelDiscovery:
        return ModelDiscovery([], cached=False)


def _client(tmp_path: Path, *roots: Path, dev_mode: bool = False) -> TestClient:
    return TestClient(
        create_app(
            provider_store=ProviderStore(tmp_path / "providers.json"),
            model_catalog=_Catalog(),
            workspace_browser=WorkspaceBrowser(roots),
            dev_mode=dev_mode,
        )
    )


def test_workspace_api_lists_one_level_sorted_directories_and_roots(tmp_path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "z-directory").mkdir()
    (first / ".hidden").mkdir()
    (first / "a-directory").mkdir()
    (first / "ordinary.txt").write_text("not returned", encoding="utf-8")
    (first / "directory-link").symlink_to(first / "a-directory", target_is_directory=True)

    response = _client(tmp_path, first, second).get(
        "/api/workspaces", params={"path": str(first)}
    )

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": 1,
        "path": str(first),
        "parent": None,
        "roots": [str(first), str(second)],
        "entries": [
            {
                "name": ".hidden",
                "path": str(first / ".hidden"),
                "type": "directory",
            },
            {
                "name": "a-directory",
                "path": str(first / "a-directory"),
                "type": "directory",
            },
            {
                "name": "z-directory",
                "path": str(first / "z-directory"),
                "type": "directory",
            },
        ],
        "truncated": False,
    }


def test_workspace_api_defaults_to_first_root_and_limits_to_500(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    for index in range(502):
        (root / f"directory-{index:03}").mkdir()

    response = _client(tmp_path, root).get("/api/workspaces")

    assert response.status_code == 200
    body = response.json()
    assert body["path"] == str(root)
    assert len(body["entries"]) == 500
    assert body["entries"][0]["name"] == "directory-000"
    assert body["entries"][-1]["name"] == "directory-499"
    assert body["truncated"] is True


@pytest.mark.parametrize(
    ("path_builder", "code"),
    [
        (lambda root: str(root / "missing"), "WORKSPACE_NOT_FOUND"),
        (lambda root: str(root.parent / "sibling"), "WORKSPACE_OUTSIDE_ROOT"),
        (lambda root: f"{root}/child/../", "WORKSPACE_OUTSIDE_ROOT"),
        (lambda root: f"{root}-prefix", "WORKSPACE_OUTSIDE_ROOT"),
    ],
)
def test_workspace_api_rejects_missing_and_escape_paths(
    tmp_path,
    path_builder,
    code,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "child").mkdir()
    (tmp_path / "sibling").mkdir()
    (tmp_path / "root-prefix").mkdir()

    response = _client(tmp_path, root).get(
        "/api/workspaces", params={"path": path_builder(root)}
    )

    assert response.status_code in {400, 404}
    assert response.json()["error"]["code"] == code


def test_workspace_api_rejects_symlink_navigation(tmp_path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "linked"
    link.symlink_to(outside, target_is_directory=True)

    response = _client(tmp_path, root).get(
        "/api/workspaces", params={"path": str(link)}
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "WORKSPACE_SYMLINK_NOT_ALLOWED"


def test_workspace_api_reports_inaccessible_directory(tmp_path, monkeypatch) -> None:
    root = tmp_path / "root"
    denied = root / "denied"
    root.mkdir()
    denied.mkdir()
    real_scandir = os.scandir

    def guarded_scandir(path):
        if Path(path) == denied:
            raise PermissionError("test denial")
        return real_scandir(path)

    monkeypatch.setattr("agent.host.workspace.os.scandir", guarded_scandir)
    response = _client(tmp_path, root).get(
        "/api/workspaces", params={"path": str(denied)}
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "WORKSPACE_NOT_ACCESSIBLE"
    assert "test denial" not in response.text


def test_development_cors_allows_only_exact_vite_origin(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    client = _client(tmp_path, root, dev_mode=True)

    allowed = client.options(
        "/api/workspaces",
        headers={
            "Origin": "http://127.0.0.1:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    rejected = client.options(
        "/api/workspaces",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert allowed.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
    assert "access-control-allow-origin" not in rejected.headers
