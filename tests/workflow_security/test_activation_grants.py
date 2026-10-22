"""Activation grants stay manual, off by default and least-privilege."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = (ROOT / "infra" / "activation-grants.bicep").read_text(encoding="utf-8")
SECRETS_USER = "4633458b-17de-408a-b874-0445c86b69e6"


def resource_block(symbol):
    match = re.search(rf"^resource {symbol} .*?^}}", TEMPLATE, re.S | re.M)
    assert match, symbol
    return match.group(0)


def test_no_workflow_or_script_deploys_the_grants():
    for path in list((ROOT / ".github").rglob("*")) + list((ROOT / "scripts").rglob("*")):
        if path.is_file():
            assert "activation-grants" not in path.read_text(encoding="utf-8", errors="ignore"), path


def test_every_grant_is_off_by_default():
    flags = re.findall(r"^param (grant\w+) bool = (\w+)$", TEMPLATE, re.M)
    assert dict(flags) == {
        "grantNotificationSubscriptionsReader": "false",
        "grantSubscriptionWebhookSecret": "false",
        "grantDashboardSigninTestSecret": "false",
        "grantDashboardLocalAuthSecret": "false",
    }


def test_dashboard_grant_is_one_test_secret_for_the_dashboard_identity_only():
    secret = resource_block("dashboardSigninTestSecret")
    assert "name: 'dashboard-auth-signin-test'" in secret and "existing" in secret
    grant = resource_block("dashboardSigninTestSecretUser")
    assert "if (grantDashboardSigninTestSecret)" in grant
    assert "scope: dashboardSigninTestSecret\n" in grant
    assert "principalId: dashboardIdentity.properties.principalId" in grant
    assert "keyVaultSecretsUserRoleId" in grant and f"'{SECRETS_USER}'" in TEMPLATE
    assert "name: '${namePrefix}-dashboard-id'" in resource_block("dashboardIdentity")
    # The production secret is neither granted nor required.
    assert "dashboard-auth-production" not in TEMPLATE


def test_no_grant_is_scoped_to_the_whole_vault_or_account():
    scopes = re.findall(r"^\s+scope: (\S+)$", TEMPLATE, re.M)
    assert sorted(scopes) == ["dashboardLocalAuthSecret", "dashboardSigninTestSecret", "subscriptionsScope", "webhookSecret"]


def test_local_auth_grant_is_one_secret_for_the_dashboard_identity_only():
    assert "name: 'dashboard-local-auth'" in resource_block("dashboardLocalAuthSecret")
    grant = resource_block("dashboardLocalAuthSecretUser")
    assert "if (grantDashboardLocalAuthSecret)" in grant and "scope: dashboardLocalAuthSecret\n" in grant
    assert "principalId: dashboardIdentity.properties.principalId" in grant and "keyVaultSecretsUserRoleId" in grant
    assert "var subscriptionsScope = '${cosmos.id}/dbs/incidents/colls/subscriptions'" in TEMPLATE
