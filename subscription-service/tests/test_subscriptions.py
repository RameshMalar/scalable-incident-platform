from unittest.mock import AsyncMock

import pytest

from tests.conftest import headers


def test_crud_and_server_fields(client, payload):
    created = client.post("/api/v1/subscriptions", json=payload, headers=headers())
    assert created.status_code == 201
    item = created.json()
    assert item["tenant_id"] == "TENANT-A"
    assert item["enabled"] is True
    assert item["created_at"] == item["updated_at"]
    assert item["created_at"].endswith("Z")
    path = created.headers["Location"]
    assert client.get(path, headers=headers()).json() == item
    assert client.get("/api/v1/subscriptions", headers=headers()).json()["items"] == [item]
    changed = client.patch(path, json={"enabled": False, "event_type": "helmet_missing"}, headers=headers())
    assert changed.status_code == 200
    assert changed.json()["enabled"] is False
    assert changed.json()["event_type"] == "helmet_missing"
    assert changed.json()["created_at"] == item["created_at"]
    assert changed.json()["updated_at"] >= item["updated_at"]
    assert client.delete(path, headers=headers()).status_code == 204
    assert client.get(path, headers=headers()).status_code == 404


def test_list_excludes_other_tenants(client, payload):
    for tenant in ["A", "B"]:
        client.post("/api/v1/subscriptions", json=payload, headers=headers(tenant))
    for tenant in ["A", "B"]:
        items = client.get("/api/v1/subscriptions", headers=headers(tenant)).json()["items"]
        assert len(items) == 1
        assert items[0]["tenant_id"] == f"TENANT-{tenant}"


@pytest.mark.parametrize("method", ["get", "patch", "delete"])
def test_foreign_id_is_indistinguishable_from_missing(client, payload, method):
    created = client.post("/api/v1/subscriptions", json=payload, headers=headers("B"))
    path = created.headers["Location"]
    kwargs = {"json": {"enabled": False}} if method == "patch" else {}
    foreign = getattr(client, method)(path, headers=headers("A"), **kwargs)
    missing = getattr(client, method)("/api/v1/subscriptions/SUB-missing", headers=headers("A"), **kwargs)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json() == {"detail": "subscription not found"}
    assert client.get(path, headers=headers("B")).json() == created.json()


@pytest.mark.parametrize("field,value", [("tenant_id", "TENANT-B"), ("id", "chosen"), ("created_at", "2026-01-01"), ("telegram_bot_token", "secret"), ("site_id", "S"), ("severity", "high"), ("analytics_type", "intrusion")])
def test_create_rejects_unsupported_and_server_fields(client, payload, field, value):
    assert client.post("/api/v1/subscriptions", json={**payload, field: value}, headers=headers()).status_code == 422


@pytest.mark.parametrize("changes", [{}, {"enabled": None}, {"tenant_id": "TENANT-B"}, {"id": "changed"}, {"channel": "email"}, {"event_type": " "}, {"telegram_chat_id": "https://example.com"}])
def test_invalid_patch(client, payload, changes):
    path = client.post("/api/v1/subscriptions", json=payload, headers=headers()).headers["Location"]
    assert client.patch(path, json=changes, headers=headers()).status_code == 422


@pytest.mark.parametrize("changes", [{"channel": "email"}, {"event_type": ""}, {"telegram_chat_id": "@somebody"}, {"telegram_chat_id": ""}, {"enabled": "false"}, {"enabled": 1}])
def test_invalid_create(client, payload, changes):
    assert client.post("/api/v1/subscriptions", json={**payload, **changes}, headers=headers()).status_code == 422


def test_pagination(client, payload):
    for _ in range(3):
        client.post("/api/v1/subscriptions", json=payload, headers=headers())
    first = client.get("/api/v1/subscriptions?limit=2", headers=headers()).json()
    second = client.get(f"/api/v1/subscriptions?limit=2&offset={first['next_offset']}", headers=headers()).json()
    assert len(first["items"]) == 2
    assert len(second["items"]) == 1
    assert second["next_offset"] is None
    assert len({item["id"] for item in first["items"] + second["items"]}) == 3


@pytest.mark.parametrize("offset,limit,expected_next", [
    (9900, 100, 10000),
    (9999, 1, 10000),
    (9950, 100, None),
    (10000, 1, None),
    (10000, 100, None),
])
def test_pagination_continuation_stays_within_offset_boundary(client, store, payload, offset, limit, expected_next):
    created = client.post("/api/v1/subscriptions", json=payload, headers=headers()).json()
    item = store.items[("TENANT-A", created["id"])]
    # Simulate a large registry with a lookahead row, without creating 10,000 records.
    store.list = AsyncMock(return_value=[item] * (limit + 1))
    response = client.get(f"/api/v1/subscriptions?limit={limit}&offset={offset}", headers=headers())
    assert response.status_code == 200
    page = response.json()
    assert page["items"] == [created] * limit
    assert page["next_offset"] == expected_next
    store.list.assert_awaited_once_with("TENANT-A", limit + 1, offset)
    if expected_next is not None:
        final = client.get(f"/api/v1/subscriptions?limit={limit}&offset={expected_next}", headers=headers())
        assert final.status_code == 200
        assert final.json()["items"] == [created] * limit
        assert final.json()["next_offset"] is None


@pytest.mark.parametrize("offset", [0, 10000])
@pytest.mark.parametrize("item_count", [0, 1, 2])
def test_pagination_final_page_has_no_continuation(client, store, payload, offset, item_count):
    created = client.post("/api/v1/subscriptions", json=payload, headers=headers()).json()
    item = store.items[("TENANT-A", created["id"])]
    store.list = AsyncMock(return_value=[item] * item_count)
    response = client.get(f"/api/v1/subscriptions?limit=2&offset={offset}", headers=headers())
    assert response.status_code == 200
    assert response.json() == {"items": [created] * item_count, "next_offset": None}
    store.list.assert_awaited_once_with("TENANT-A", 3, offset)


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1", "offset=10001", "tenant_id=TENANT-B"])
def test_query_bounds_and_tenant_override(client, payload, query):
    for tenant in ["A", "B"]:
        client.post("/api/v1/subscriptions", json=payload, headers=headers(tenant))
    response = client.get(f"/api/v1/subscriptions?{query}", headers=headers())
    if query.startswith("tenant_id"):
        assert response.status_code == 200
        assert len(response.json()["items"]) == 1
        assert response.json()["items"][0]["tenant_id"] == "TENANT-A"
    else:
        assert response.status_code == 422


def test_roles(client, payload):
    assert client.get("/api/v1/subscriptions", headers=headers("reader")).status_code == 200
    assert client.post("/api/v1/subscriptions", json=payload, headers=headers("reader")).status_code == 403
    assert client.get("/api/v1/subscriptions", headers=headers("producer")).status_code == 403
    assert client.post("/api/v1/subscriptions", json=payload, headers=headers("producer")).status_code == 403
    path = client.post("/api/v1/subscriptions", json=payload, headers=headers()).headers["Location"]
    assert client.patch(path, json={"enabled": False}, headers=headers("reader")).status_code == 403
    assert client.delete(path, headers=headers("reader")).status_code == 403
    assert client.get(path, headers=headers()).json()["enabled"] is True


@pytest.mark.parametrize("method,path,body", [("get", "/api/v1/subscriptions", None), ("post", "/api/v1/subscriptions", "create"), ("get", "/api/v1/subscriptions/SUB-x", None), ("patch", "/api/v1/subscriptions/SUB-x", "patch"), ("delete", "/api/v1/subscriptions/SUB-x", None)])
def test_all_crud_requires_authentication(client, payload, method, path, body):
    kwargs = {"json": payload if body == "create" else {"enabled": False}} if body else {}
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
