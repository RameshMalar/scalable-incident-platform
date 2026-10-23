"""Business dashboard for the incident store.

Reads directly from the same Cosmos DB account the worker writes to
(`worker-service/app/store.py`). `core.bicep` sets `disableLocalAuth: true` on
that account, so there is no primary key to hand a dashboard -- the only way in
is an Azure AD credential holding a data-plane role. Run this with an identity
that has been granted **Cosmos DB Built-in Data Reader** (not the worker's
Data Contributor role -- a dashboard never needs to write). Locally that's
`az login` as a user with that role assignment; deployed, it's a managed
identity with the same role.
"""

import base64
import json
import time
from html import escape
from urllib.error import HTTPError, URLError

import logging
import os
from io import BytesIO
from pathlib import Path
from PIL import Image, UnidentifiedImageError
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential

from evidence import EvidenceStore, EvidenceUnavailable
from dashboard_security import (
    LOCAL_ROLE_NAMES, LOCAL_ROLE_PERMISSIONS, AccessDenied, OperatorContext, OperatorVerifier, access_token, id_token,
)
from account_setup import render_account_setup
from access_api import AccessAPIUnavailable, current_grant
from access_roles import InvalidAccess
from admin_access import render_admin_access
from analytics_catalog import SEVERITIES, load_catalog, valid_custom_name, humanise
import local_auth
import simulator
import telegram_subscribers

logger = logging.getLogger("dashboard")

# LML SENTINEL brand, sampled from the artwork in assets/.
PRODUCT_NAME = "LML SENTINEL"
TAGLINE = "Always Watching. Always Ready."
ACADEMIC_TITLE = local_auth.ACADEMIC_TITLE
BRAND_CYAN = "#0cf0fe"
ASSETS = Path(__file__).with_name("assets")
EMBLEM = ASSETS / "lml-sentinel-emblem.png"
FAVICON = ASSETS / "lml-sentinel-favicon.png"
BANNER = ASSETS / "lml-sentinel-banner.jpg"

# Magnitude charts (per type, per site, per day) are one series, so they take a
# single accent -- the shield cyan -- rather than a hue per category.
ACCENT = "#00d2fe"
SERIES_COLORS = [ACCENT, "#d95926", "#199e70", "#9085e9"]
# Severity is a state, not an identity, so it takes the status palette. Every
# adjacent pair clears normal-vision Delta E 15 and CVD Delta E 13 on SURFACE
# (validated), and severity is always shown with its own shape and label too.
SEVERITY_COLORS = {"critical": "#d03b3b", "high": "#ec835a", "medium": "#f2cc3d", "low": "#7d93b2"}
SEVERITY_ICONS = {"critical": "■", "high": "▲", "medium": "◆", "low": "●"}
SEVERITY_ORDER = ["critical", "high", "medium", "low"]
TREND_WORSE, TREND_BETTER = "#ec835a", "#0ca30c"
TEST_PURPOSES = ("e2e_test", "load_test")

DATE_RANGE_PRESETS = {
    "Last 7 days": 7,
    "Last 30 days": 30,
    "Last 3 months": 90,
    "Last 6 months": 182,
    "Last year": 365,
}

PAGE_BG = "#001227"
SURFACE = "#082039"
GRIDLINE = "#1d3a5e"
# Matches .streamlit/config.toml textColor. The custom panels always draw a
# dark background, so they also set their own text colour rather than
# inheriting it from whatever Streamlit theme is active.
INK_PRIMARY = "#edf5ff"
INK_SECONDARY = "#d0dceb"
INK_MUTED = "#9bafc7"


@st.cache_resource
def get_credential():
    # AZURE_CLIENT_ID selects the dashboard's user-assigned managed identity.
    return DefaultAzureCredential()


@st.cache_resource
def get_database():
    endpoint = os.environ.get("COSMOS_ENDPOINT")
    if not endpoint:
        st.error(
            "COSMOS_ENDPOINT is not set, e.g. "
            "https://incident-cosmos-xxxx.documents.azure.com:443/"
        )
        st.stop()
    client = CosmosClient(endpoint, credential=get_credential())
    return client.get_database_client(os.environ.get("COSMOS_DATABASE", "incidents"))


@st.cache_resource
def get_container():
    return get_database().get_container_client(os.environ.get("COSMOS_CONTAINER", "incidents"))


@st.cache_resource
def get_operator_verifier():
    return OperatorVerifier.from_env()


def operator_tokens():
    mode = os.environ.get("DASHBOARD_AUTH_MODE", "oidc")
    if mode == "oidc":
        return st.user.tokens if getattr(st.user, "is_logged_in", False) else {}
    if mode == "easyauth":
        return None
    # Local sign-in has no Entra token, so nothing can act for a user upstream.
    raise AccessDenied("Unsupported authentication mode")


def local_operator_context(username: str, role: str = local_auth.VIEWER) -> OperatorContext:
    """The approved platform tenants, with the permissions of the account's role."""
    policy = json.loads((Path(__file__).with_name("access-policy.json")).read_text(encoding="utf-8"))
    tenants = sorted({tenant for entry in policy["users"].values() for tenant in entry["tenants"]})
    return OperatorContext(f"local:{username}", policy["directory_id"], tuple(tenants),
                           LOCAL_ROLE_PERMISSIONS[role], int(time.time()) + local_auth.IDLE_SECONDS,
                           LOCAL_ROLE_NAMES[role])


def operator_display_name(context) -> str:
    user = str(getattr(context, "user_id", ""))
    return user.removeprefix("local:") if user.startswith("local:") else "Microsoft account"


def delegated_api_token():
    require_operator()
    return access_token(st.context.headers, operator_tokens())


def require_operator():
    if os.environ.get("DASHBOARD_AUTH_MODE", "oidc") == "local":
        # Draws the sign-in form and stops the page until signed in.
        account = local_auth.require_signed_in(st.context.headers, banner=str(BANNER) if BANNER.exists() else None)
        return local_operator_context(account.username, account.role)
    try:
        verifier=get_operator_verifier()
        tokens=operator_tokens()
        identity_token=id_token(st.context.headers,tokens)
        if os.environ.get('ENABLE_CENTRAL_ACCESS','').lower() == 'true':
            identity=verifier.verify_identity(identity_token)
            # Fresh authoritative access on every protected operation, including
            # cache hits. Registry failure never falls back to packaged grants.
            grant=current_grant(identity,access_token(st.context.headers,tokens),verifier.approved_tenants)
            return OperatorContext(identity.user_id,identity.directory_id,grant.tenants,grant.permissions,identity.expires_at,grant.role.value)
        return verifier.verify(identity_token)
    except (AccessDenied, AccessAPIUnavailable, InvalidAccess, ValueError, TypeError, OSError, KeyError):
        st.subheader(ACADEMIC_TITLE)
        st.info("Sign in with your authorized Microsoft account. Access is denied until your identity and platform tenant permissions are verified.")
        if os.environ.get("DASHBOARD_AUTH_MODE", "oidc") == "easyauth":
            st.link_button("Sign in with Microsoft", "/.auth/login/aad")
        else:
            try:
                configured = "auth" in st.secrets and "microsoft" in st.secrets["auth"]
            except (FileNotFoundError, KeyError):
                configured = False
            if configured and st.button("Sign in with Microsoft"):
                st.login("microsoft")
        st.stop()


def ordered_incidents(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.sort_values(["timestamp", "tenant_id", "event_id"], ascending=[False, True, True], kind="stable")


def load_incidents() -> pd.DataFrame:
    # Authentication runs even on cache hits. The tenant tuple is a cache key.
    return _load_incidents(require_operator().tenants)


@st.cache_resource(ttl=300, max_entries=2)
def _load_incidents(tenants: tuple[str, ...]) -> pd.DataFrame:
    # Shared read-only snapshots avoid repeatedly unpickling the full tenant
    # dataset for each evidence check. Callers never mutate this DataFrame.
    container = get_container()
    items = []
    for tenant in tenants:
        # Consume all continuation pages; no TOP/LIMIT silently changes KPI totals.
        items.extend(container.query_items(
            query="SELECT * FROM c WHERE c.tenant_id = @tenant",
            parameters=[{"name": "@tenant", "value": tenant}],
            partition_key=tenant,
        ))
    refreshed_at = datetime.now(timezone.utc).isoformat()
    if not items:
        df = pd.DataFrame(columns=["event_id", "tenant_id", "site_id", "analytics_type", "event_type", "severity", "timestamp", "evidence_uri"])
    else:
        df = pd.DataFrame(items)
        if not df["tenant_id"].isin(tenants).all():
            raise AccessDenied("Unexpected foreign-tenant incident data")
    df["timestamp"] = pd.to_datetime(df["timestamp"], format="ISO8601", utc=True)
    df = ordered_incidents(df)
    df.attrs["refreshed_at"] = refreshed_at
    return df


@st.cache_resource
def get_evidence_store() -> EvidenceStore | None:
    return EvidenceStore.from_env()


def load_evidence(evidence_uri: str, thumbnail: bool = True) -> bytes | None:
    context = require_operator()
    if get_evidence_store() is None:
        return None
    records = _load_incidents(context.tenants)
    known = ("evidence_uri" in records and records["evidence_uri"].eq(evidence_uri).any())
    if not isinstance(evidence_uri, str) or not (known or evidence_uri in demo_evidence_in_session()):
        return None
    return (_load_preview if thumbnail else _load_original)(context.tenants, evidence_uri)


# --- Demo mode: incidents that exist only in this signed-in session ------------------------
DEMO_KEY = "demo_incidents"
MAX_DEMO_INCIDENTS = 50


def demo_incidents() -> list[dict]:
    return st.session_state.setdefault(DEMO_KEY, [])


def demo_evidence_in_session() -> frozenset:
    """Evidence URIs of this session's demo incidents that are packaged pool photographs."""
    allowed = simulator.demo_evidence_uris(simulator.load_evidence_pools(), os.environ.get("EVIDENCE_BLOB_ENDPOINT", ""),
                                           os.environ.get("EVIDENCE_CONTAINER", "evidence"))
    return frozenset(i["evidence_uri"] for i in demo_incidents() if i.get("evidence_uri") in allowed)


def with_demo_incidents(frame: pd.DataFrame) -> pd.DataFrame:
    """Real incidents plus this session's demo incidents, marked demo=True. Never written anywhere."""
    demos = demo_incidents()
    if not demos:
        return frame
    demo_frame = pd.DataFrame(demos)
    demo_frame["timestamp"] = pd.to_datetime(demo_frame["timestamp"], format="ISO8601", utc=True)
    demo_frame["demo"] = True
    merged = ordered_incidents(pd.concat([demo_frame, frame.assign(demo=False)], ignore_index=True))
    merged.attrs = dict(frame.attrs)
    return merged


def is_demo(row) -> bool:
    return row.get("demo") is True


@st.cache_data(ttl=60, max_entries=2, show_spinner=False)
def _load_original(tenant_scope: tuple[str, ...], evidence_uri: str) -> bytes | None:
    store = get_evidence_store()
    if store is None:
        return None
    try:
        data = store.fetch(evidence_uri).data
    except EvidenceUnavailable:
        logger.warning("Evidence unavailable; rendering incident without image")
        return None
    # The detail dialog hands these bytes straight to st.image, so a corrupt or
    # mislabelled blob must become "no image" here rather than a page error.
    try:
        with Image.open(BytesIO(data)) as image:
            image.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        logger.warning("Evidence image could not be decoded")
        return None
    return data


@st.cache_data(ttl=600, max_entries=48, show_spinner=False)
def _load_preview(tenant_scope: tuple[str, ...], evidence_uri: str) -> bytes | None:
    # The full Blob is downloaded on a cache miss, but only the preview remains
    # in the long-lived cache. Originals have a separate two-entry cache.
    data = _load_original(tenant_scope, evidence_uri)
    if not data:
        return None
    try:
        with Image.open(BytesIO(data)) as image:
            if image.width * image.height > 16_000_000:
                return None
            image.thumbnail((640, 360))
            output = BytesIO()
            image.convert("RGB").save(output, format="JPEG", quality=80)
            return output.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        logger.warning("Evidence image could not be decoded")
        return None


def inject_css() -> None:
    st.markdown(
        f"""
        <style>
        .block-container {{ padding-top: 3rem; max-width: 1600px; }}
        .stApp {{ background: {PAGE_BG}; }}
        h1, h2, h3 {{ letter-spacing: -0.02em; }}
        [data-testid="stSidebar"] {{ border-right: 1px solid rgba(255,255,255,0.08); }}
        [data-testid="stSidebarUserContent"] > [data-testid="stVerticalBlock"] {{ min-height:calc(100dvh - 110px); }}
        .st-key-sentinel_signout {{ margin-top:auto; padding-top:24px; padding-bottom:8px; }}

        [data-testid="stPlotlyChart"] {{ border:1px solid {GRIDLINE}; border-radius:10px; overflow:hidden; }}
        @media(max-width:1000px) {{
          .st-key-overview_analytics [data-testid="stHorizontalBlock"], .st-key-overview_operations > [data-testid="stVerticalBlock"] > [data-testid="stHorizontalBlock"] {{ flex-wrap:wrap; }}
          .st-key-overview_analytics [data-testid="stColumn"], .st-key-overview_operations > [data-testid="stVerticalBlock"] > [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {{ min-width:min(300px,100%); flex:1 1 300px; }}
        }}
        .soc-header {{
            display:flex; flex-wrap:wrap; gap:12px 24px; align-items:flex-end; justify-content:space-between;
            padding: 4px 0 14px; margin-bottom: 14px; border-bottom: 1px solid {GRIDLINE};
        }}
        .soc-eyebrow {{ font-size:12px; letter-spacing:.12em; text-transform:uppercase; color:{INK_MUTED}; }}
        .soc-title {{ font-size:28px; font-weight:750; color:{INK_PRIMARY}; line-height:1.2; margin-top:2px; }}
        .soc-chips {{ display:flex; flex-wrap:wrap; gap:8px; }}
        .chip {{
            display:inline-flex; align-items:center; gap:6px; padding:4px 10px; border-radius:999px;
            font-size:12.5px; color:{INK_SECONDARY}; background:{SURFACE}; border:1px solid rgba(255,255,255,0.10);
            white-space:nowrap;
        }}
        .chip.role-admin {{ color:{INK_PRIMARY}; border-color:{ACCENT}; background:rgba(0,210,254,0.14); font-weight:650; }}
        .chip.role-viewer {{ color:{INK_PRIMARY}; font-weight:650; }}
        .chip.demo-chip {{ color:#1a1300; background:#f2cc3d; border-color:#f2cc3d; font-weight:750; letter-spacing:.04em; }}
        .chip .live {{ width:7px; height:7px; border-radius:50%; background:{TREND_BETTER}; }}
        .soc-filters {{ font-size:12.5px; color:{INK_MUTED}; margin:-6px 0 14px; }}
        .brand {{ display:flex; align-items:center; gap:14px; }}
        .brand-emblem {{ width:64px; height:auto; border-radius:10px; filter: drop-shadow(0 0 10px rgba(0,210,254,0.35)); }}
        .brand-name {{ font-size:26px; font-weight:800; letter-spacing:.04em; color:#ffffff; line-height:1.1; }}
        .brand-name span {{ color:{BRAND_CYAN}; }}
        .brand-tagline {{ font-size:13px; color:{INK_SECONDARY}; letter-spacing:.05em; margin:2px 0 4px; }}
        .side-brand {{ margin:-6px 0 12px; }}
        .side-brand .brand-name {{ font-size:18px; }}
        .side-brand .brand-tagline {{ font-size:11.5px; color:{INK_MUTED}; }}
        @media(max-width:600px) {{ .brand-emblem {{ width:44px; }} .brand-name {{ font-size:20px; }} }}

        .soc-kpis {{ display:grid; grid-template-columns:repeat(8,minmax(0,1fr)); gap:10px; margin-bottom:12px; }}
        .soc-kpis .kpi-card {{ border-top:3px solid var(--accent); min-height:116px; box-shadow:0 0 16px rgba(0,210,254,.07); }}
        @media(max-width:1400px) {{ .soc-kpis {{ grid-template-columns:repeat(4,minmax(0,1fr)); }} }}
        @media(max-width:900px) {{ .soc-kpis {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} }}
        @media(max-width:600px) {{ .soc-kpis {{ grid-template-columns:1fr; }} .soc-title {{ font-size:22px; }} }}

        .kpi-card {{
            min-width:0;
            background: {SURFACE}; color: {INK_PRIMARY}; border: 1px solid rgba(255,255,255,0.10);
            border-radius: 12px; padding: 14px 16px;
        }}
        .kpi-top {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }}
        .kpi-icon {{
            width: 30px; height: 30px; border-radius: 8px;
            display: flex; align-items: center; justify-content: center; font-size: 14px;
        }}
        .kpi-trend {{ font-size: 12px; font-variant-numeric: tabular-nums; font-weight:600; }}
        .kpi-trend.flat {{ color: {INK_MUTED}; font-weight:400; }}
        .kpi-label {{ font-size: 12.5px; color: {INK_SECONDARY}; margin-bottom: 2px; }}
        .kpi-value {{ font-size: clamp(16px,1.6vw,24px); font-weight: 700; font-variant-numeric: tabular-nums; overflow-wrap:anywhere; }}
        .kpi-sub {{ font-size: 11.5px; color: {INK_MUTED}; margin-top: 4px; }}

        .panel {{
            background: {SURFACE}; color: {INK_PRIMARY}; border: 1px solid rgba(255,255,255,0.10);
            border-radius: 12px; padding: 18px 18px 6px;
        }}
        .panel h3 {{ margin: 0 0 16px; font-size: 15px; color: {INK_PRIMARY}; }}

        .bar-row {{ display: flex; align-items: center; gap: 10px; font-size: 13.5px; margin-bottom: 12px; }}
        .bar-row .name {{ width: 110px; color: {INK_SECONDARY}; flex-shrink: 0; }}
        .bar-track {{ flex: 1; height: 8px; background: {GRIDLINE}; border-radius: 4px; overflow: hidden; }}
        .bar-fill {{ height: 100%; border-radius: 4px; }}
        .bar-row .count {{ width: 64px; text-align: right; font-variant-numeric: tabular-nums; color:{INK_SECONDARY}; }}

        .sev-pill {{
            display:inline-flex; align-items:center; gap:5px; padding:2px 9px; border-radius:999px;
            font-size:11.5px; font-weight:700; letter-spacing:.04em; color:{INK_PRIMARY};
            background: color-mix(in srgb, var(--sev) 22%, transparent); border:1px solid var(--sev);
        }}
        .sev-pill .shape {{ color: var(--sev); }}
        .sev-summary {{ display:flex; flex-wrap:wrap; gap:8px; margin: 2px 0 10px; }}

        .insight-row {{ display: flex; gap: 10px; padding: 10px 0; border-bottom: 1px solid {GRIDLINE}; }}
        .insight-row:last-child {{ border-bottom: none; }}
        .insight-dot {{ width: 8px; height: 8px; border-radius: 50%; margin-top: 5px; flex-shrink: 0; }}
        .insight-row .t {{ font-size: 13.5px; margin-bottom: 2px; }}
        .insight-row .d {{ font-size: 12.5px; color: {INK_MUTED}; }}

        .flow {{ display:inline-flex; flex-wrap:wrap; gap:6px; align-items:center; padding:8px 12px; border-radius:10px;
                 background:{SURFACE}; border:1px solid rgba(255,255,255,0.10); color:{INK_PRIMARY}; font-weight:600; margin:2px 0 8px; }}
        .flow span {{ color:{ACCENT}; }}
        .user-card {{ background:{SURFACE}; border:1px solid rgba(255,255,255,0.10); border-radius:10px; padding:10px 12px; margin-bottom:8px; }}
        .user-card .u {{ font-weight:650; color:{INK_PRIMARY}; overflow-wrap:anywhere; }}
        .user-card .m {{ font-size:12px; color:{INK_MUTED}; margin-top:2px; }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def analytics_name(value) -> str:
    # Display name only; the stored value is never rewritten (analytics_catalog.json).
    return load_catalog().analytics_name(value)


def event_name(value) -> str:
    return load_catalog().event_name(value)


@st.cache_data(show_spinner=False)
def _image_data_uri(path: str) -> str:
    try:
        data = Path(path).read_bytes()
    except OSError:
        return ""
    kind = "jpeg" if path.endswith(".jpg") else "png"
    return f"data:image/{kind};base64," + base64.b64encode(data).decode("ascii")


def severity_badge(severity, count: int | None = None) -> str:
    severity = str(severity).lower()
    color = SEVERITY_COLORS.get(severity, INK_MUTED)
    icon = SEVERITY_ICONS.get(severity, "○")
    suffix = f' <span style="font-variant-numeric:tabular-nums">{count}</span>' if count is not None else ""
    return (f'<span class="sev-pill" style="--sev:{color}"><span class="shape">{icon}</span>'
            f'{escape(severity.upper())}{suffix}</span>')


def kpi_card(label: str, value: str, icon: str, color: str, delta: float | None,
             sub: str = "", higher_is_worse: bool = True) -> str:
    if delta is None:
        trend_html = '<span class="kpi-trend flat">—</span>'
    elif delta == 0:
        trend_html = '<span class="kpi-trend flat">flat</span>'
    else:
        # More incidents is bad news, so "up" is not painted as good.
        worse = (delta > 0) == higher_is_worse
        arrow = "↑" if delta > 0 else "↓"
        trend_html = (f'<span class="kpi-trend" style="color:{TREND_WORSE if worse else TREND_BETTER}">'
                      f'{arrow} {abs(delta):.0f}%</span>')
    sub_html = f'<div class="kpi-sub">{escape(sub)}</div>' if sub else ""
    return (
        f'<div class="kpi-card" style="--accent:{color}">'
        f'<div class="kpi-top"><div class="kpi-icon" style="background:{color}26; color:{color}">{icon}</div>{trend_html}</div>'
        f'<div class="kpi-label">{escape(label)}</div>'
        f'<div class="kpi-value">{escape(value)}</div>{sub_html}</div>'
    )


# A percentage against a handful of incidents reads as alarming and means little;
# below this base the card shows the previous count instead of a percentage.
MIN_TREND_BASE = 10


def pct_change(current: int, previous: int) -> float | None:
    if previous < MIN_TREND_BASE:
        return None
    return (current - previous) / previous * 100


def bar_panel(title: str, counts: pd.Series, color_map: dict) -> str:
    max_count = counts.max() if len(counts) else 1
    total = counts.sum() if len(counts) else 0
    rows = ""
    for name, count in counts.items():
        width = int(count / max_count * 100) if max_count else 0
        color = color_map.get(name, SERIES_COLORS[0])
        share = f" · {count / total * 100:.0f}%" if total else ""
        rows += (
            f'<div class="bar-row"><span class="name">{escape(str(name))}</span>'
            f'<div class="bar-track"><div class="bar-fill" style="width:{width}%; background:{color}"></div></div>'
            f'<span class="count">{int(count)}{share}</span></div>'
        )
    return f'<div class="panel"><h3>{escape(title)}</h3>{rows}</div>'


def relative_time(ts: pd.Timestamp) -> str:
    delta = datetime.now(timezone.utc) - ts.to_pydatetime()
    seconds = delta.total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def is_test_purpose(value) -> bool:
    return value in TEST_PURPOSES


# Stored timestamps stay UTC; people read Singapore time (no daylight saving).
SGT = timezone(timedelta(hours=8), "SGT")
DATASETS = ("Operational incidents", "Include load-test traffic")


def fmt_time(ts, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert(SGT).strftime(fmt) + " SGT"


def to_display_time(frame: pd.DataFrame) -> pd.DataFrame:
    shown = frame.assign(timestamp=frame["timestamp"].dt.tz_convert(SGT))
    shown.attrs = dict(frame.attrs)
    return shown


def load_test_mask(frame: pd.DataFrame) -> pd.Series:
    """Load-test traffic, classified as the Worker and Notification service do:
    event_purpose "load_test", or -- for events from before event_purpose
    existed -- no purpose and an EVT-LOAD- event ID."""
    purpose = frame["event_purpose"] if "event_purpose" in frame else pd.Series(None, index=frame.index, dtype=object)
    legacy = purpose.isna() & frame["event_id"].astype(str).str.startswith("EVT-LOAD-")
    return (purpose == "load_test") | legacy


def purpose_label(purpose, event_id, demo=False) -> str:
    if demo is True:
        return "DEMO · session only"
    if purpose == "load_test" or (not isinstance(purpose, str) and str(event_id).startswith("EVT-LOAD-")):
        return "Load test"
    return f"TEST · {purpose}" if is_test_purpose(purpose) else "Incident"


def render_latest_incidents(filtered: pd.DataFrame, limit: int = 8) -> None:
    require_operator()
    st.subheader("Latest persisted incidents")
    st.caption("Newest first · Azure Cosmos DB records, plus DEMO incidents from this session · times in SGT")
    if filtered.empty:
        st.info("No persisted incidents match these filters.")
        return
    for position, (_, row) in enumerate(ordered_incidents(filtered).head(limit).iterrows()):
        with st.container(border=True):
            image_col, detail_col = st.columns([1, 3])
            uri = row.get("evidence_uri")
            image = load_evidence(uri) if isinstance(uri, str) and uri else None
            with image_col:
                if image:
                    st.image(image, width="stretch")
                    if st.button("Expand image", key=f"feed_{position}_{row['tenant_id']}_{row['event_id']}"):
                        _evidence_detail_dialog(row, image)
                else:
                    st.caption("Evidence unavailable" if uri else "No evidence image")
            with detail_col:
                st.markdown(severity_badge(row["severity"]), unsafe_allow_html=True)
                st.text(f"{event_name(row['event_type'])} · {analytics_name(row['analytics_type'])}")
                st.caption(f"Tenant {row['tenant_id']} · Site {row['site_id']} · Camera/source {row.get('source_id', 'Unavailable')}")
                st.caption(fmt_time(row["timestamp"]))
                purpose = row.get("event_purpose", "incident")
                st.caption("DEMO · this session only, not stored" if is_demo(row)
                           else f"TEST · {purpose}" if is_test_purpose(purpose) else "Purpose: incident")
                st.text(f"Event ID: {row['event_id']}")


def key_insights_panel(filtered: pd.DataFrame, previous: pd.DataFrame) -> str:
    insights = []

    total = len(filtered)
    serious = int(filtered["severity"].isin(["critical", "high"]).sum())
    if total:
        share = serious / total * 100
        insights.append((
            SEVERITY_COLORS["critical"],
            "Critical and high share",
            f"{serious} of {total} incidents ({share:.0f}%) in the selected range.",
        ))

    if total and filtered["tenant_id"].nunique():
        top_tenant = filtered["tenant_id"].value_counts().idxmax()
        top_count = filtered["tenant_id"].value_counts().max()
        insights.append((
            ACCENT,
            f"{top_tenant} is the top contributor",
            f"{top_count} incidents, {top_count / total * 100:.0f}% of total volume.",
        ))

    if total and "site_id" in filtered and filtered["site_id"].nunique():
        sites = filtered["site_id"].value_counts()
        insights.append((
            ACCENT,
            f"Busiest site: {sites.idxmax()}",
            f"{sites.max()} incidents, {sites.max() / total * 100:.0f}% of total volume.",
        ))

    prev_total = len(previous)
    change = pct_change(total, prev_total)
    if change is not None:
        direction = "up" if change > 0 else "down" if change < 0 else "flat"
        insights.append((
            TREND_WORSE if change > 0 else TREND_BETTER,
            f"Volume trending {direction}",
            f"{change:+.0f}% vs. the previous period of equal length.",
        ))

    rows = "".join(
        f'<div class="insight-row"><span class="insight-dot" style="background:{color}"></span>'
        f'<div><div class="t">{escape(title)}</div><div class="d">{escape(detail)}</div></div></div>'
        for color, title, detail in insights
    )
    if not rows:
        rows = f'<p style="color:{INK_MUTED}; font-size:13px;">Not enough data for insights.</p>'
    return f'<div class="panel"><h3>Key insights</h3>{rows}</div>'


def _style_figure(fig: go.Figure, title: str, height: int) -> go.Figure:
    fig.update_layout(
        title=dict(text=title, font=dict(size=15, color=INK_PRIMARY), x=0.01),
        paper_bgcolor=SURFACE, plot_bgcolor=SURFACE,
        font=dict(color=INK_SECONDARY, size=12),
        margin=dict(l=10, r=16, t=48, b=10), height=height,
        hoverlabel=dict(bgcolor=PAGE_BG, bordercolor=GRIDLINE, font_color=INK_PRIMARY),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, font=dict(color=INK_SECONDARY)),
    )
    fig.update_xaxes(showgrid=False, linecolor=GRIDLINE, tickfont=dict(color=INK_MUTED))
    fig.update_yaxes(gridcolor=GRIDLINE, zeroline=False, tickfont=dict(color=INK_MUTED))
    return fig


def daily_severity_figure(filtered: pd.DataFrame) -> go.Figure:
    days = filtered["timestamp"].dt.tz_convert(SGT).dt.floor("D")
    counts = (filtered.assign(day=days).groupby(["day", "severity"]).size().unstack(fill_value=0))
    full = pd.date_range(days.min(), days.max(), freq="D", tz=days.dt.tz)
    counts = counts.reindex(full, fill_value=0)
    fig = go.Figure()
    # Critical sits on the baseline, where it is easiest to compare day to day.
    for severity in SEVERITY_ORDER + sorted(set(counts.columns) - set(SEVERITY_ORDER)):
        if severity not in counts:
            continue
        fig.add_bar(
            x=counts.index, y=counts[severity], name=severity.title(),
            marker=dict(color=SEVERITY_COLORS.get(severity, INK_MUTED), line=dict(color=SURFACE, width=1.5)),
            hovertemplate=f"%{{y}} {escape(severity)}<extra></extra>",
        )
    fig.update_layout(barmode="stack", bargap=0.25, barcornerradius=4, hovermode="x unified",
                      legend_traceorder="normal")
    fig.update_xaxes(hoverformat="%a %d %b %Y")
    return _style_figure(fig, "Incidents per day by severity (SGT)", 280)


def ranked_bar_figure(counts: pd.Series, title: str, noun: str) -> go.Figure:
    counts = counts.sort_values()
    fig = go.Figure(go.Bar(
        x=counts.values, y=[str(label) for label in counts.index], orientation="h",
        marker=dict(color=ACCENT), text=counts.values, textposition="outside",
        textfont=dict(color=INK_SECONDARY), cliponaxis=False,
        hovertemplate=f"%{{y}}: %{{x}} {noun}<extra></extra>",
    ))
    fig.update_layout(barcornerradius=4, bargap=0.35, showlegend=False)
    fig.update_xaxes(showgrid=True, gridcolor=GRIDLINE, showticklabels=False)
    fig.update_yaxes(showgrid=False, tickfont=dict(color=INK_SECONDARY))
    return _style_figure(fig, title, max(220, 70 + 34 * len(counts)))


WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def activity_heatmap_figure(filtered: pd.DataFrame) -> go.Figure:
    grid = (filtered.assign(weekday=filtered["timestamp"].dt.tz_convert(SGT).dt.dayofweek, hour=filtered["timestamp"].dt.tz_convert(SGT).dt.hour)
            .groupby(["weekday", "hour"]).size().unstack(fill_value=0)
            .reindex(index=range(7), columns=range(24), fill_value=0))
    fig = go.Figure(go.Heatmap(
        z=grid.values, x=[f"{hour:02d}" for hour in range(24)], y=WEEKDAYS,
        # One hue, dark (quiet) to bright (busy) on the dark surface.
        colorscale=[[0, "#0b2744"], [0.5, "#0a7fa8"], [1, "#5fe6ff"]],
        xgap=2, ygap=2, colorbar=dict(title=dict(text="Incidents", font=dict(color=INK_MUTED)), tickfont=dict(color=INK_MUTED), thickness=10),
        hovertemplate="%{y} %{x}:00 SGT · %{z} incidents<extra></extra>",
    ))
    fig.update_yaxes(autorange="reversed", showgrid=False)
    fig.update_xaxes(title=dict(text="Hour of day (SGT)", font=dict(color=INK_MUTED)))
    return _style_figure(fig, "Incident Heatmap (Hour × Weekday)", 300)


def overview_kpis(filtered: pd.DataFrame, previous: pd.DataFrame, now: datetime | None = None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    period = "previous period"
    count = lambda frame, severity: int((frame["severity"] == severity).sum())

    def card(label, value, prev_value, icon, color, unit):
        sub = f"{prev_value} in the {period}" if prev_value is not None else unit
        return kpi_card(label, str(value), icon, color,
                        pct_change(value, prev_value) if prev_value is not None else None, sub)

    total = len(filtered)
    last_day = int(((filtered["timestamp"] > now - timedelta(hours=24)) & (filtered["timestamp"] <= now)).sum())
    day_before = int(((filtered["timestamp"] > now - timedelta(hours=48))
                      & (filtered["timestamp"] <= now - timedelta(hours=24))).sum())
    latest = filtered["timestamp"].max()
    cameras = filtered["source_id"].nunique() if "source_id" in filtered else 0
    return [
        card("Total Incidents", total, len(previous), "◆", ACCENT, ""),
        card("Critical", count(filtered, "critical"), count(previous, "critical"), SEVERITY_ICONS["critical"], SEVERITY_COLORS["critical"], ""),
        card("High", count(filtered, "high"), count(previous, "high"), SEVERITY_ICONS["high"], SEVERITY_COLORS["high"], ""),
        card("Medium", count(filtered, "medium"), count(previous, "medium"), SEVERITY_ICONS["medium"], SEVERITY_COLORS["medium"], ""),
        kpi_card("Last 24 hours", str(last_day), "◷", ACCENT, None, "within selected range · last 24 hours"),
        kpi_card("Sites", str(filtered["site_id"].nunique()), "⌂", ACCENT,
                 pct_change(filtered["site_id"].nunique(), previous["site_id"].nunique()),
                 f"across {filtered['tenant_id'].nunique()} tenants"),
        kpi_card("Cameras Reporting", str(cameras), "◉", ACCENT, None, "distinct sources in range"),
        kpi_card("Latest Incident", fmt_time(latest, "%d %b %H:%M")[:-4] if pd.notna(latest) else "No incidents", "↻", ACCENT, None,
                 relative_time(latest) if pd.notna(latest) else "nothing in range"),
    ]


def severity_mix_figure(filtered: pd.DataFrame) -> go.Figure:
    counts = filtered["severity"].value_counts()
    order = [s for s in SEVERITY_ORDER if s in counts] + sorted(set(counts.index) - set(SEVERITY_ORDER))
    fig = go.Figure(go.Pie(labels=[s.title() for s in order], values=[int(counts[s]) for s in order],
                           hole=.68, sort=False, textinfo="percent", textposition="inside",
                           marker=dict(colors=[SEVERITY_COLORS.get(s, INK_MUTED) for s in order]),
                           hovertemplate="%{label}: %{value} (%{percent})<extra></extra>"))
    fig.add_annotation(text=f"<b>{len(filtered):,}</b><br>Incidents", x=.5, y=.5, showarrow=False,
                       font=dict(color=INK_PRIMARY, size=18))
    return _style_figure(fig, "Severity Mix", 300)


def render_latest_overview(filtered: pd.DataFrame) -> None:
    st.markdown("**Latest Incidents**")
    for position, (_, row) in enumerate(ordered_incidents(filtered).head(5).iterrows()):
        image_col, text_col, action_col = st.columns([1, 4, 1])
        uri = row.get("evidence_uri")
        image = load_evidence(uri) if isinstance(uri, str) and uri else None
        with image_col:
            if image:
                st.image(image, width="stretch")
            else:
                st.caption("No image")
        with text_col:
            st.markdown(f"{escape(analytics_name(row['analytics_type']))} {severity_badge(row['severity'])}", unsafe_allow_html=True)
            st.caption(f"{row['site_id']} · {row.get('source_id', '—')} · {relative_time(row['timestamp'])}")
        with action_col:
            with st.popover("›", help="Incident details"):
                render_incident_detail(row)


def render_overview(filtered: pd.DataFrame, previous: pd.DataFrame, simulate_page=None) -> None:
    require_operator()
    st.subheader("Overview")
    cards = overview_kpis(filtered, previous)
    st.markdown('<div class="soc-kpis">' + ''.join(cards) + '</div>', unsafe_allow_html=True)
    st.caption("Selected operational range · SGT · trends compare equal periods, with a previous count of at least 10.")
    if filtered.empty:
        st.info("No incidents match the current filters.")
        return
    with st.container(key="overview_analytics"):
        day_col, type_col, severity_col = st.columns([1.2, 1.1, 1], gap="small")
        with day_col:
            st.plotly_chart(daily_severity_figure(filtered), width="stretch", config={"displayModeBar": False})
        with type_col:
            by_type = filtered["analytics_type"].map(analytics_name).value_counts()
            fig = ranked_bar_figure(by_type, "Incidents by Analytics Type", "incidents")
            fig.update_layout(height=300)
            st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
        with severity_col:
            st.plotly_chart(severity_mix_figure(filtered), width="stretch", config={"displayModeBar": False})
    with st.container(key="overview_operations"):
        heat_col, site_col, latest_col = st.columns([1.2, 1, 1.2], gap="small")
        with heat_col:
            st.plotly_chart(activity_heatmap_figure(filtered), width="stretch", config={"displayModeBar": False})
        with site_col:
            fig = ranked_bar_figure(filtered["site_id"].value_counts().head(5), "Top 5 Sites by Incidents", "incidents")
            fig.update_layout(height=300)
            st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
        with latest_col:
            with st.container(border=True, height=340):
                render_latest_overview(filtered)


PURPOSE_FILTERS = ("All purposes", "Real incidents", "Tests only")
EVIDENCE_FILTERS = ("Any evidence", "With image", "Without image")
SORT_ORDERS = ("Newest first", "Oldest first", "Most severe first")
SEARCH_COLUMNS = ("event_id", "tenant_id", "site_id", "source_id", "analytics_type", "event_type", "description")
MAX_SEARCH_LENGTH = 100


def filter_incidents(frame: pd.DataFrame, query: str = "", severities=None, purpose: str = PURPOSE_FILTERS[0],
                     evidence: str = EVIDENCE_FILTERS[0], sort: str = SORT_ORDERS[0]) -> pd.DataFrame:
    """The All incidents page's search and filters, as a pure function."""
    result = frame
    query = (query or "").strip()[:MAX_SEARCH_LENGTH].lower()
    if query:
        # Literal, case-insensitive substring match; user text is never a regex.
        hit = pd.Series(False, index=result.index)
        for column in SEARCH_COLUMNS:
            if column in result:
                hit |= result[column].astype("string").str.lower().str.contains(query, regex=False, na=False)
        hit |= result["analytics_type"].map(analytics_name).str.lower().str.contains(query, regex=False, na=False)
        if "event_type" in result:
            hit |= result["event_type"].map(event_name).str.lower().str.contains(query, regex=False, na=False)
        result = result[hit]
    if severities is not None:
        result = result[result["severity"].isin(severities)]
    purposes = result["event_purpose"] if "event_purpose" in result else pd.Series("incident", index=result.index)
    tests = purposes.isin(TEST_PURPOSES) | load_test_mask(result)
    if purpose == PURPOSE_FILTERS[1]:
        result = result[~tests]
    elif purpose == PURPOSE_FILTERS[2]:
        result = result[tests]
    has_image = (result["evidence_uri"].apply(lambda uri: isinstance(uri, str) and bool(uri))
                 if "evidence_uri" in result else pd.Series(False, index=result.index))
    if evidence == EVIDENCE_FILTERS[1]:
        result = result[has_image]
    elif evidence == EVIDENCE_FILTERS[2]:
        result = result[~has_image]
    if sort == SORT_ORDERS[1]:
        return result.sort_values(["timestamp", "tenant_id", "event_id"], kind="stable")
    if sort == SORT_ORDERS[2]:
        rank = result["severity"].map({sev: i for i, sev in enumerate(SEVERITY_ORDER)}).fillna(len(SEVERITY_ORDER))
        return ordered_incidents(result).assign(_rank=rank).sort_values("_rank", kind="stable").drop(columns="_rank")
    return ordered_incidents(result)


def incident_table(frame: pd.DataFrame) -> pd.DataFrame:
    column = lambda name, default="": frame[name] if name in frame else pd.Series(default, index=frame.index)
    return pd.DataFrame({
        "Time (SGT)": frame["timestamp"].dt.tz_convert(SGT).dt.tz_localize(None),
        "Severity": frame["severity"].map(lambda sev: f"{SEVERITY_ICONS.get(str(sev).lower(), '○')} {str(sev).upper()}"),
        "Event ID": frame["event_id"],
        "Analytics": frame["analytics_type"].map(analytics_name),
        "Event": column("event_type").map(event_name),
        "Tenant": frame["tenant_id"],
        "Site": column("site_id"),
        "Camera": column("source_id"),
        "Purpose": [purpose_label(p, e, d) for p, e, d in zip(column("event_purpose", None), frame["event_id"], column("demo", False))],
        "Evidence": column("evidence_uri", None).map(lambda uri: isinstance(uri, str) and bool(uri)),
    })


CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def incidents_csv(frame: pd.DataFrame) -> bytes:
    """CSV export with spreadsheet formula injection neutralised."""
    def safe(value):
        return "'" + value if isinstance(value, str) and value.startswith(CSV_FORMULA_PREFIXES) else value
    export = frame.drop(columns=[c for c in ("_rid", "_self", "_etag", "_attachments", "_ts") if c in frame])
    # Element-wise, whatever dtype pandas inferred for a text column.
    return export.map(safe).to_csv(index=False).encode("utf-8")


def render_incident_detail(row: pd.Series) -> None:
    with st.container(border=True):
        image_col, detail_col = st.columns([1, 2])
        uri = row.get("evidence_uri")
        image = load_evidence(uri) if isinstance(uri, str) and uri else None
        with image_col:
            if image:
                st.image(image, width="stretch")
                if st.button("Expand image", key=f"detail_{row['tenant_id']}_{row['event_id']}"):
                    _evidence_detail_dialog(row, image)
            else:
                st.caption("Evidence unavailable" if uri else "No evidence image")
        with detail_col:
            st.markdown(severity_badge(row["severity"]), unsafe_allow_html=True)
            st.markdown(f"**{escape(str(row['event_id']))}** · {escape(analytics_name(row['analytics_type']))} · {escape(event_name(row.get('event_type', '')))}")
            if isinstance(row.get("description"), str):
                st.text(row["description"])
            st.caption(f"Tenant {row['tenant_id']} · Site {row.get('site_id', '')} · Camera/source {row.get('source_id', 'Unavailable')}")
            st.caption(fmt_time(row["timestamp"]) + f" · {relative_time(row['timestamp'])}")


def render_all_incidents(filtered: pd.DataFrame) -> None:
    context = require_operator()
    st.subheader("All incidents")
    search_col, severity_col = st.columns([2, 2])
    with search_col:
        query = st.text_input("Search", placeholder="Event ID, site, camera, analytics or event type",
                              max_chars=MAX_SEARCH_LENGTH, key="incident_search")
    with severity_col:
        present = [sev for sev in SEVERITY_ORDER if sev in set(filtered["severity"])]
        severities = st.multiselect("Severity", present, default=present, key="incident_severity",
                                    format_func=lambda sev: f"{SEVERITY_ICONS.get(sev, '○')} {sev.title()}")
    purpose_col, evidence_col, sort_col = st.columns(3)
    with purpose_col:
        purpose = st.selectbox("Purpose", PURPOSE_FILTERS, key="incident_purpose")
    with evidence_col:
        evidence = st.selectbox("Evidence", EVIDENCE_FILTERS, key="incident_evidence")
    with sort_col:
        sort = st.selectbox("Sort", SORT_ORDERS, key="incident_sort")

    matches = filter_incidents(filtered, query, severities, purpose, evidence, sort)
    counts = matches["severity"].value_counts()
    st.markdown('<div class="sev-summary">' + "".join(
        severity_badge(sev, int(counts.get(sev, 0))) for sev in SEVERITY_ORDER
    ) + '</div>', unsafe_allow_html=True)
    st.caption(f"{len(matches)} of {len(filtered)} incidents match. Select a row to see its details and evidence.")

    selection = st.dataframe(
        incident_table(matches), width="stretch", hide_index=True, height=min(560, 38 + 35 * max(len(matches), 1)),
        on_select="rerun", selection_mode="single-row", key="incident_table",
        column_config={
            "Time (SGT)": st.column_config.DatetimeColumn(format="YYYY-MM-DD HH:mm:ss"),
            "Evidence": st.column_config.CheckboxColumn(help="Incident references an evidence image"),
        },
    )
    rows = getattr(getattr(selection, "selection", None), "rows", None) or []
    if rows and rows[0] < len(matches):
        render_incident_detail(matches.iloc[rows[0]])

    if getattr(context, "can_export", False):
        st.download_button(
            "Export matching incidents (CSV)", incidents_csv(matches),
            file_name=f"incidents-{datetime.now(timezone.utc):%Y%m%d-%H%M}.csv", mime="text/csv",
            disabled=matches.empty,
        )
    else:
        st.caption("CSV export is available to Admins.")


@st.dialog("Evidence", width="large")
def _evidence_detail_dialog(row: pd.Series, image: bytes) -> None:
    require_operator()
    full = load_evidence(row["evidence_uri"], thumbnail=False)
    st.image(full or image, width="stretch")
    st.markdown(f"**{row['event_id']}**")
    st.caption(
        f"{analytics_name(row['analytics_type'])} · {row['severity']} · "
        f"{relative_time(row['timestamp'])} · tenant {row['tenant_id']} · "
        f"site {row['site_id']}"
    )
    purpose = row.get("event_purpose", "incident")
    if purpose in ("e2e_test", "load_test"):
        st.caption(f"TEST evidence · {purpose}")


def _render_evidence_grid(
    subset: pd.DataFrame, total_in_range: int, page_size: int, state_key: str
) -> None:
    signature_key = f"evidence_snapshot_{state_key}"
    signature = int(pd.util.hash_pandas_object(ordered_incidents(subset)[["timestamp", "tenant_id", "event_id"]], index=False).sum())
    shown_key = f"evidence_shown_{state_key}"
    if st.session_state.get(signature_key) != signature:
        st.session_state[signature_key] = signature
        st.session_state[shown_key] = page_size
    if shown_key not in st.session_state:
        st.session_state[shown_key] = page_size

    shown = min(st.session_state[shown_key], max(page_size, ((len(subset) + page_size - 1) // page_size) * page_size))
    st.session_state[shown_key] = shown
    newest = ordered_incidents(subset).iloc[max(0, shown - page_size):shown]

    st.caption(
        f"{len(subset)} of {total_in_range} incidents in range reference "
        "an image." + (f" Page ending at incident {min(shown, len(subset))}." if len(subset) > shown else "")
    )

    # Three across keeps a camera still legible without making the page a scroll
    # of full-width photographs.
    columns = st.columns(3)
    for position, (_, row) in enumerate(newest.iterrows()):
        image = load_evidence(row["evidence_uri"])

        with columns[position % 3]:
            if image is None:
                st.warning(f"{row['event_id']} — evidence could not be read.")
                continue

            st.image(
                image,
                caption=(
                    f"{row['event_id']} · {analytics_name(row['analytics_type'])} · "
                    f"{row['severity']} · {relative_time(row['timestamp'])} · "
                    + ("DEMO" if is_demo(row) else "TEST" if row.get("event_purpose") in ("e2e_test", "load_test") else "Incident")
                ),
                width="stretch",
            )
            if st.button(
                "Expand", key=f"expand_{state_key}_{row['tenant_id']}_{row['event_id']}", width="stretch"
            ):
                _evidence_detail_dialog(row, image)

    def change_page(offset: int) -> None:
        st.session_state[shown_key] = max(page_size, shown + offset)

    if shown > page_size:
        st.button("Previous page", key=f"previous_{state_key}", on_click=change_page, args=(-page_size,))

    if len(subset) > shown:
        st.button("Next page", key=f"more_{state_key}", on_click=change_page, args=(page_size,))


def render_evidence(filtered: pd.DataFrame, page_size: int = 12) -> None:
    require_operator()
    st.subheader("AI Analytics Evidence Gallery")
    st.caption("Stored incident evidence · these images are not live video or proof of a live inference pipeline.")

    store = get_evidence_store()

    if store is None:
        st.info(
            "Evidence is not configured for this deployment. Set "
            "`EVIDENCE_BLOB_ENDPOINT` to the storage account's blob endpoint."
        )
        return

    if "evidence_uri" not in filtered.columns:
        st.info("No incident in the store carries an evidence reference.")
        return

    with_evidence = filtered[filtered["evidence_uri"].apply(lambda uri: isinstance(uri, str) and store.describes_evidence(uri))]

    if with_evidence.empty:
        st.info(
            "No incident in the current filters references an image in "
            f"`{store.container}`. Simulated PPE incidents, and any from a "
            "local run of `/api/v1/simulate`, carry placeholder paths rather "
            "than real blob references, so they appear here without an image."
        )
        return

    # Render only the chosen category; Streamlit tabs would eagerly load every grid.
    categories = sorted(with_evidence["analytics_type"].dropna().unique().tolist())
    category = st.selectbox("Analytics category", ["All"] + categories,
                            format_func=lambda value: value if value == "All" else analytics_name(value))
    subset = with_evidence if category == "All" else with_evidence[with_evidence["analytics_type"] == category]
    _render_evidence_grid(subset, len(filtered), page_size, state_key=category)


def checkbox_filter(label: str, options: list, key: str) -> list:
    opt_keys = {opt: f"{key}_opt_{opt}" for opt in options}
    for opt, opt_key in opt_keys.items():
        if opt_key not in st.session_state:
            st.session_state[opt_key] = True

    selected = [opt for opt in options if st.session_state[opt_keys[opt]]]

    all_key = f"{key}_all"
    st.session_state[all_key] = len(selected) == len(options)

    def _on_all_change():
        value = st.session_state[all_key]
        for opt_key in opt_keys.values():
            st.session_state[opt_key] = value

    with st.expander(f"{label} ({len(selected)}/{len(options)})"):
        st.checkbox("All", key=all_key, on_change=_on_all_change)
        for opt in options:
            st.checkbox(str(opt), key=opt_keys[opt])

    return [opt for opt in options if st.session_state[opt_keys[opt]]]


SIM_SITES = ("SITE-01", "SITE-02", "SITE-03")
SIM_CAMERAS = ("CAM-SIM-01", "CAM-SIM-02", "CAM-SIM-03")
CUSTOM_CHOICE = "Custom analytics…"
SIM_MIN_INTERVAL_SECONDS = 30
SIM_CHECK_INTERVAL_SECONDS = 5


@st.cache_data(ttl=60, show_spinner=False)
def _load_subscribers(tenants: tuple[str, ...]) -> list:
    return telegram_subscribers.load_subscribers(get_database(), tenants)


def subscribers_for(tenants) -> list | None:
    """The Telegram routing data for these tenants, or None if it cannot be read."""
    try:
        return _load_subscribers(tuple(tenants))
    except Exception:  # read-only view; the Worker remains the authority
        logger.warning("Telegram subscriber data unavailable")
        return None


def ingestion_token(scope: str) -> str:
    # The dashboard's own managed identity; no user token or secret is involved.
    return get_credential().get_token(scope).token


def incident_persisted(event_id: str, tenant_id: str) -> bool:
    """Point read of one incident in its tenant partition."""
    try:
        get_container().read_item(item=event_id, partition_key=tenant_id)
        return True
    except Exception:
        return False


def _recipient_names(people) -> str:
    return ", ".join(f"{p.label or p.kind} ({p.chat})" for p in people)


DEMO_MIN_INTERVAL_SECONDS = 3


def demo_tenant(context) -> str:
    preferred = os.environ.get("SIMULATOR_TENANT", "TENANT-01")
    return preferred if preferred in context.tenants else context.tenants[0]


INGESTION_SIMULATOR_URL = "https://incident-ingestion.calmmushroom-4789bfc0.malaysiawest.azurecontainerapps.io/"


def render_ingestion_launcher() -> None:
    context = require_operator()
    if not context.can_simulate:
        st.error("Simulation permission is required.")
        return
    st.subheader("Simulate Event")
    st.link_button("Open Simulate Event", INGESTION_SIMULATOR_URL, icon=":material/open_in_new:")


def render_simulator() -> None:
    context = require_operator()
    if not context.can_simulate:
        st.error("Simulation permission is required.")
        return
    demo = simulator.demo_mode()
    st.subheader("Simulate Event")
    if demo:
        st.caption("Creates a TEST incident in your signed-in session only. It appears on every page marked DEMO, "
                   "is never sent to ingestion or stored in Cosmos DB, and no Telegram message is sent.")
        config, tenant = None, demo_tenant(context)
    else:
        st.caption("Sends one TEST incident through the authorized ingestion API. It is stored, appears on every page "
                   "marked TEST, and alerts only the Telegram subscribers of that event type.")
        try:
            config = simulator.simulator_config()
        except simulator.SimulatorUnavailable as exc:
            st.info(str(exc))
            return
        if config.tenant not in context.tenants:
            st.error("The simulator's tenant is outside your authorized scope.")
            return
        tenant = config.tenant

    catalog = load_catalog()
    specs = {spec.display_name: spec for spec in catalog.analytics}
    form_col, info_col = st.columns([3, 2])
    with form_col:
        choice = st.selectbox("Analytics", [*specs, CUSTOM_CHOICE], key="sim_analytics")
        if choice == CUSTOM_CHOICE:
            analytics_type = st.text_input("Custom analytics type", placeholder="e.g. vehicle_wrong_way_detection",
                                           max_chars=48, key="sim_custom_type").strip()
            event_type = st.text_input("Custom event type", placeholder="e.g. wrong_way_driving",
                                       max_chars=48, key="sim_custom_event").strip()
            default_severity, pool = "medium", None
            description = f"{humanise(event_type)} detected in monitored area" if event_type else ""
            valid = valid_custom_name(analytics_type) and valid_custom_name(event_type)
        else:
            spec = specs[choice]
            events = {event.display_name: event for event in spec.events}
            event = events[st.selectbox("Event", list(events), key=f"sim_event_{spec.type}")]
            analytics_type, event_type, default_severity = spec.type, event.type, event.severity
            description, pool, valid = event.description, event.evidence_pool, True
        description = st.text_area("Description", value=description, max_chars=1000,
                                   key=f"sim_description_{analytics_type}_{event_type}")
        severity = st.selectbox("Severity", SEVERITIES, index=SEVERITIES.index(default_severity),
                                format_func=lambda sev: f"{SEVERITY_ICONS[sev]} {sev.title()}",
                                key=f"sim_severity_{analytics_type}_{event_type}")
        site = st.selectbox("Site", SIM_SITES, key="sim_site")
        pool_items = simulator.load_evidence_pools().get(pool) if pool else None
        cameras = sorted({item["source_id"] for item in pool_items}) if pool_items else list(SIM_CAMERAS)
        camera = st.selectbox("Camera / source", cameras, key=f"sim_camera_{pool or 'none'}")
        if choice == CUSTOM_CHOICE and (analytics_type or event_type) and not valid:
            st.error("Custom names use lowercase letters, digits and underscores: 3–48 characters, starting with a letter.")

    pools = simulator.load_evidence_pools()
    people = None
    with info_col:
        with st.container(border=True):
            st.markdown("**Telegram**")
            subscribers = subscribers_for((tenant,))
            if subscribers is None:
                st.warning("Subscriber list unavailable. Delivery is decided by the Worker's routing.")
            elif not valid:
                st.caption("Enter a valid event type to see its subscribers.")
            else:
                people = telegram_subscribers.recipients(subscribers, tenant, event_type)
                if people:
                    st.success(f"Would notify {len(people)}: {_recipient_names(people)}")
                else:
                    st.info("No eligible subscribers for this event type.")
            if demo:
                st.caption("From the real routing records. DEMO incidents are never delivered.")
            st.markdown("**Evidence**")
            if pool and pools.get(pool) and os.environ.get("EVIDENCE_BLOB_ENDPOINT"):
                st.caption(f"A stored photograph from {camera} ({pool} set) is attached.")
            else:
                st.caption("No evidence image for this type; the alert is text only.")
            st.caption(f"Tenant {tenant} · purpose e2e_test · one incident per confirmation")

    with st.form("simulate_event", clear_on_submit=True):
        st.markdown(f"{severity_badge(severity)} &nbsp; **{escape(catalog.analytics_name(analytics_type) if analytics_type else '—')}** · "
                    f"{escape(catalog.event_name(event_type) if event_type else '—')} · {escape(tenant)} · {escape(site)}",
                    unsafe_allow_html=True)
        acknowledged = st.checkbox("Create exactly one DEMO incident in this session." if demo
                                   else "Send exactly one TEST event and alert its subscribers.")
        submitted = st.form_submit_button("Create demo incident" if demo else "Send test event", type="primary",
                                          disabled=not valid)

    if submitted:
        now = time.monotonic()
        interval = DEMO_MIN_INTERVAL_SECONDS if demo else SIM_MIN_INTERVAL_SECONDS
        if not demo and st.session_state.get("simulator_pending"):
            st.warning("Resolve the previous event before sending another.")
        elif not acknowledged:
            st.error("Tick the confirmation first.")
        elif now - st.session_state.get("simulator_last_attempt", -interval) < interval:
            st.warning(f"Wait {interval} seconds between events.")
        else:
            st.session_state["simulator_last_attempt"] = now
            event = simulator.build_event(
                analytics_type=analytics_type, event_type=event_type, severity=severity, description=description,
                tenant_id=tenant, site_id=site, evidence_pool=pools.get(pool) if pool else None,
                evidence_endpoint=os.environ.get("EVIDENCE_BLOB_ENDPOINT", ""),
                evidence_container=os.environ.get("EVIDENCE_CONTAINER", "evidence"),
                id_prefix=simulator.DEMO_PREFIX if demo else "EVT-SIM-", source_id=camera)
            if demo:
                incidents = demo_incidents()
                incidents.insert(0, event)
                del incidents[MAX_DEMO_INCIDENTS:]
                st.session_state["demo_last"] = {"event_id": event["event_id"],
                                                 "alerted": _recipient_names(people) if people else ""}
                logger.info("demo incident created in session event_id=%s", event["event_id"])
                st.rerun()  # so the header, KPIs and every page include it at once
            else:
                _send_live(config, event)

    if demo:
        last = st.session_state.get("demo_last")
        if last:
            st.success(f"DEMO incident {last['event_id']} created in this session. It now appears on Overview, "
                       "All incidents and Evidence, marked DEMO. Nothing was sent to ingestion or stored in Cosmos DB.")
            st.info(f"Telegram: would notify {last['alerted']}. Not sent: DEMO incidents are never delivered."
                    if last["alerted"] else "Telegram: no eligible subscribers for this event type. Nothing is sent in demo mode.")
        count = len(demo_incidents())
        if count and st.button(f"Clear {count} demo incident{'s' if count != 1 else ''}", key="sim_clear_demo"):
            st.session_state[DEMO_KEY] = []
            st.session_state.pop("demo_last", None)
            st.rerun()
        return

    pending = st.session_state.get("simulator_pending")
    if pending:
        event_id = pending["event_id"]
        if pending["state"] == "stored":
            st.success(f"Stored in Cosmos DB: {event_id}. It now appears on Overview, All incidents and Evidence.")
        elif pending["state"] == "accepted":
            if incident_persisted(event_id, pending["tenant_id"]):
                pending["state"] = "stored"
                _load_incidents.clear()
                st.success(f"Stored in Cosmos DB: {event_id}. It now appears on Overview, All incidents and Evidence.")
            else:
                st.info(f"Ingestion accepted {event_id}; waiting for the Worker to store it.")
                if st.button("Check again", key="sim_check"):
                    st.rerun()
        else:
            st.warning(f"The outcome of {event_id} is unknown. It is not retried automatically.")
        if st.button("Send another event", key="sim_reset"):
            st.session_state.pop("simulator_pending", None)
            st.rerun()


def _send_live(config, event) -> None:
    """The real path: one authorized POST to ingestion (DASHBOARD_SIMULATOR_MODE=live only)."""
    try:
        token = ingestion_token(config.scope)
    except Exception:
        logger.warning("simulator could not obtain an ingestion token")
        st.error("The dashboard identity could not obtain an ingestion token. Nothing was sent.")
        return
    # Kept across reruns so an ambiguous outcome is never retried automatically.
    st.session_state["simulator_pending"] = {"event_id": event["event_id"], "tenant_id": config.tenant,
                                             "state": "unknown", "checked": 0.0}
    try:
        simulator.publish(config.origin, token, event)
    except HTTPError as exc:
        if 400 <= exc.code < 500:
            # A definite refusal: nothing was published.
            st.session_state.pop("simulator_pending", None)
            logger.warning("simulator event refused status=%s", exc.code)
            st.error(f"Ingestion refused the event (HTTP {exc.code}). Nothing was published.")
        else:
            st.error("Ingestion's answer was unclear. Check All incidents before sending again.")
    except (ValueError, URLError, TimeoutError, OSError):
        st.error("Ingestion's answer was unclear. Check All incidents before sending again.")
    else:
        st.session_state["simulator_pending"]["state"] = "accepted"
        logger.info("simulator event accepted event_id=%s", event["event_id"])


@st.cache_data(ttl=60, show_spinner=False)
def _load_subscription_rows(tenants: tuple[str, ...]) -> list:
    return telegram_subscribers.load_subscription_rows(get_database(), tenants)


def subscription_rows_for(tenants) -> list | None:
    try:
        return _load_subscription_rows(tuple(tenants))
    except Exception:  # read-only view; the Worker remains the authority
        logger.warning("Telegram subscription data unavailable")
        return None


def render_telegram_subscribers() -> None:
    context = require_operator()
    if not getattr(context, "is_admin", False):
        st.error("Administrator role is required.")
        return
    st.subheader("Telegram Subscriptions")
    st.markdown(
        '<div class="flow">Incident event <span>→</span> Subscription routing <span>→</span> Worker '
        '<span>→</span> Telegram notification</div>', unsafe_allow_html=True)
    st.caption("A notification is sent only for an enabled subscription that matches the incident's tenant and exact "
               "event type, to a chat an operator has approved for that tenant. Load-test traffic is never sent. "
               "These are the real routing records; this page is read-only and DEMO incidents are never delivered.")
    rows = subscription_rows_for(context.tenants)
    if rows is None:
        st.error("Subscription data could not be read.")
        return
    eligible = [r for r in rows if r.eligible]
    st.markdown('<div class="soc-kpis">' + "".join([
        kpi_card("Subscriptions", str(len(rows)), "≡", ACCENT, None, "Telegram records in your tenants"),
        kpi_card("Eligible", str(len(eligible)), "✓", ACCENT, None, "enabled and approved"),
        kpi_card("Destinations", str(len({(r.tenant_id, r.chat) for r in eligible})), "◎", ACCENT, None, "chats that would be notified"),
        kpi_card("Event types covered", str(len({r.event_type for r in eligible})), "◆", ACCENT, None, "with an eligible subscription"),
    ]) + '</div>', unsafe_allow_html=True)
    if not rows:
        st.info("No Telegram subscription exists for your tenants.")
        return
    catalog = load_catalog()
    st.dataframe(pd.DataFrame([{
        "Subscriber": f"{r.label or r.kind} ({r.chat})", "Tenant": r.tenant_id,
        "Analytics": catalog.analytics_for_event(r.event_type), "Event type": event_name(r.event_type),
        "Enabled": r.enabled, "Operator approved": r.approved,
        "Notification": "Eligible" if r.eligible else "Ineligible",
    } for r in rows]), hide_index=True, width="stretch", column_config={
        "Enabled": st.column_config.CheckboxColumn(), "Operator approved": st.column_config.CheckboxColumn()})
    st.caption("Chat IDs are masked. Subscriptions are managed through the Subscription API and operator approval, "
               "not from this page.")


ROLE_MATRIX = pd.DataFrame(
    [
        ("View Overview, All incidents and Evidence", True, True),
        ("Search and filter incidents", True, True),
        ("Open Platform Observability (Grafana)", True, True),
        ("Export incidents as CSV", True, False),
        ("Simulate Event", True, False),
        ("Telegram subscribers (read-only)", True, False),
        ("Administration console", True, False),
    ],
    columns=["Capability", "Admin", "Viewer"],
)


def render_admin_console() -> None:
    context = require_operator()
    # Checked here as well as in navigation: a page must never rely on being hidden.
    if not getattr(context, "is_admin", False):
        st.error("Administrator role is required.")
        return
    mode = os.environ.get("DASHBOARD_AUTH_MODE", "oidc")
    st.subheader("Administration")
    st.caption("Read-only view of how access works in this deployment. No setting can be changed from here.")

    session_col, scope_col = st.columns(2)
    with session_col:
        with st.container(border=True):
            st.markdown("**Your session**")
            st.text(f"User: {operator_display_name(context)}")
            st.text(f"Role: {getattr(context, 'role', '') or 'Unassigned'}")
            st.text(f"Sign-in: {'Local username and password' if mode == 'local' else 'Microsoft Entra ID'}")
            if mode == "local":
                st.caption(f"Sessions end after {local_auth.IDLE_SECONDS // 60} idle minutes or "
                           f"{local_auth.ABSOLUTE_SECONDS // 3600} hours, and on sign out.")
    with scope_col:
        with st.container(border=True):
            st.markdown("**Data scope**")
            st.text(f"Tenants: {', '.join(context.tenants)}")
            st.text("Cosmos DB: read-only managed identity")
            st.text("Evidence: private Blob container, managed identity")

    st.markdown("**Roles and capabilities**")
    st.dataframe(ROLE_MATRIX, hide_index=True, width="stretch", column_config={
        "Admin": st.column_config.CheckboxColumn(), "Viewer": st.column_config.CheckboxColumn(),
    })

    if mode == "local":
        st.markdown("**Configured accounts**")
        try:
            accounts = local_auth.load_credentials().accounts.values()
        except local_auth.LocalAuthUnavailable:
            st.error("The sign-in file could not be read.")
        else:
            # Usernames and roles only; password hashes never leave the server.
            st.dataframe(pd.DataFrame([(a.username, LOCAL_ROLE_NAMES[a.role]) for a in accounts],
                                      columns=["Username", "Role"]), hide_index=True, width="stretch")
            st.caption("Accounts live in the Key Vault secret mounted into this revision; "
                       "change them with tools/make_password_hash.py and a new secret version.")


GRAFANA_DASHBOARD_URL = ("https://incident-grafana--sys-events-3aac3b8e.calmmushroom-4789bfc0.malaysiawest.azurecontainerapps.io"
                         "/d/incident-platform-ops/platform-observability-7c-health-and-performance"
                         "?orgId=1&from=now-6h&to=now&timezone=Asia%2FSingapore")


def render_header(context, refreshed_at: str) -> None:
    try:
        refreshed = fmt_time(pd.Timestamp(datetime.fromisoformat(refreshed_at)), "%d %b %H:%M")
    except (TypeError, ValueError):
        refreshed = "Unavailable"
    emblem = _image_data_uri(str(EMBLEM))
    emblem_img = f'<img class="brand-emblem" src="{emblem}" alt="{PRODUCT_NAME} emblem">' if emblem else ""
    st.markdown(
        '<div class="soc-header"><div class="brand">'
        f'{emblem_img}'
        '<div><div class="brand-name">LML <span>SENTINEL</span></div>'
        f'<div class="brand-tagline">{TAGLINE}</div>'
        f'<div class="soc-eyebrow">{ACADEMIC_TITLE}</div></div></div>'
        '<div class="soc-chips">'
        f'<span class="chip"><span class="live"></span>Azure Cosmos DB · refreshed {escape(refreshed)}</span>'
        '</div></div>',
        unsafe_allow_html=True,
    )


def main() -> None:
    st.set_page_config(page_title=f"{PRODUCT_NAME} · Security Operations Center",
                       page_icon=str(FAVICON) if FAVICON.exists() else "🛡️", layout="wide")
    inject_css()
    context = require_operator()
    mode = os.environ.get("DASHBOARD_AUTH_MODE", "oidc")
    if EMBLEM.exists():
        st.logo(str(EMBLEM), size="large")
    with st.sidebar:
        st.markdown(
            f'<div class="side-brand"><div class="brand-name">LML <span>SENTINEL</span></div>'
            f'<div class="brand-tagline">{TAGLINE}</div></div>',
            unsafe_allow_html=True,
        )

    navigation_slot = st.sidebar.container(key="sentinel_navigation")

    with st.sidebar:
        refresh = st.selectbox("Refresh interval", ["Manual", "5 minutes", "15 minutes"], index=0)
    interval = {"Manual": None, "5 minutes": 300, "15 minutes": 900}[refresh]

    @st.fragment(run_every=interval)
    def refresh_clock():
        require_operator()
        now = time.monotonic()
        last = st.session_state.get("soc_refresh_tick", now)
        st.session_state.setdefault("soc_refresh_tick", now)
        manual = st.button("Refresh incidents", icon=":material/refresh:", width="stretch")
        if manual and now - st.session_state.get("soc_last_manual_refresh", -60) < 60:
            st.info("Wait one minute between manual data refreshes.")
            return
        if manual:
            st.session_state["soc_last_manual_refresh"] = now
            _load_incidents.clear()
        if manual or (interval and now - last >= interval):
            st.session_state["soc_refresh_tick"] = now
            st.rerun()
    # Real Cosmos incidents plus this session's DEMO incidents, shown in Singapore time.
    df = to_display_time(with_demo_incidents(load_incidents()))

    with st.sidebar.expander("Filters", expanded=False):
        dataset = st.radio("Dataset", DATASETS, index=0, key="dataset",
                           help="Excludes records marked load_test or carrying the legacy EVT-LOAD- event ID prefix.")
        if dataset == DATASETS[0]:
            df = df[~load_test_mask(df)]

        tenants = sorted(df["tenant_id"].unique())
        tenant_filter = checkbox_filter("Tenant", tenants, "tenant")

        # Filter by display name, so stored aliases (PPE_detection, ppe_detection) are one choice.
        analytics_names = sorted({analytics_name(a) for a in df["analytics_type"].unique()})
        analytics_filter = checkbox_filter("Analytics type", analytics_names, "analytics")

        severities = [s for s in SEVERITY_ORDER if s in df["severity"].unique()] + sorted(set(df["severity"].dropna()) - set(SEVERITY_ORDER))
        severity_filter = checkbox_filter("Severity", severities, "severity")

        today = datetime.now(SGT).date()
        min_date = df["timestamp"].min().date() if not df.empty else today
        max_date = df["timestamp"].max().date() if not df.empty else today

        range_preset = st.selectbox(
            "Date range (SGT)", list(DATE_RANGE_PRESETS) + ["Custom range"], index=1
        )

        if range_preset == "Custom range":
            date_range = st.date_input(
                "Custom dates",
                value=(max(min_date, max_date - timedelta(days=30)), max_date),
                min_value=min_date,
                max_value=max_date,
                label_visibility="collapsed",
            )
            start_date, end_date = date_range if len(date_range) == 2 else (min_date, max_date)
        else:
            end_date = max_date
            start_date = max(min_date, max_date - timedelta(days=DATE_RANGE_PRESETS[range_preset] - 1))

    def apply_dims(frame: pd.DataFrame) -> pd.DataFrame:
        return frame[
            frame["tenant_id"].isin(tenant_filter)
            & frame["analytics_type"].map(analytics_name).isin(analytics_filter)
            & frame["severity"].isin(severity_filter)
        ]

    dim_filtered = apply_dims(df)
    filtered = dim_filtered[
        (dim_filtered["timestamp"].dt.date >= start_date)
        & (dim_filtered["timestamp"].dt.date <= end_date)
    ]

    period_days = (end_date - start_date).days + 1
    prev_end = start_date - timedelta(days=1)
    prev_start = prev_end - timedelta(days=period_days - 1)
    previous = dim_filtered[
        (dim_filtered["timestamp"].dt.date >= prev_start)
        & (dim_filtered["timestamp"].dt.date <= prev_end)
    ]

    # Only pages this role may use are registered, so a hidden page's URL falls
    # back to the default page; each page also re-checks its own permission.
    pages = [
        st.Page(lambda: render_overview(filtered, previous), title="Overview",
                icon=":material/dashboard:", default=True, url_path="overview"),
        st.Page(lambda: render_all_incidents(filtered), title="All Incidents", icon=":material/table_rows:",
                url_path="all-incidents"),
        st.Page(lambda: render_evidence(filtered), title="Evidence Gallery", icon=":material/photo_library:",
                url_path="evidence"),
    ]
    if getattr(context, "is_admin", False):
        pages.append(st.Page(render_telegram_subscribers, title="Telegram Subscriptions",
                             icon=":material/notifications_active:", url_path="telegram"))
    if mode != "local":
        # Account Setup acts for the person through their Entra token, which local sign-in does not have.
        pages.append(st.Page(lambda: render_account_setup(require_operator, delegated_api_token),
                             title="Account Setup", icon=":material/person:", url_path="account-setup"))
    monitor_pages = pages[:3]
    operation_pages = pages[3:]
    pg = st.navigation(pages, position="hidden")
    with navigation_slot:
        st.caption("MONITOR")
        for page in monitor_pages:
            st.page_link(page)
        st.caption("OPERATIONS")
        if context.can_simulate:
            st.link_button("Simulate Event", INGESTION_SIMULATOR_URL, icon=":material/science:")
        for page in operation_pages:
            st.page_link(page)
        st.link_button("Platform Observability", GRAFANA_DASHBOARD_URL, icon=":material/monitoring:", width="stretch")
    with st.sidebar.container(key="sentinel_signout"):
        if mode == "easyauth":
            st.link_button("Sign out", "/.auth/logout", icon=":material/logout:", width="stretch")
        elif mode == "local":
            if st.button("Sign out", icon=":material/logout:", width="stretch"):
                local_auth.sign_out()
                st.rerun()
        elif st.button("Sign out", icon=":material/logout:", width="stretch"):
            st.logout()


    header_col, refresh_col = st.columns([6, 1], vertical_alignment="center")
    with header_col:
        render_header(context, df.attrs.get("refreshed_at", ""))
    with refresh_col:
        refresh_clock()
    st.markdown(
        f'<div class="soc-filters">{escape(dataset)} · Tenants: {escape(", ".join(tenant_filter) or "None")} · '
        f'Analytics: {escape(", ".join(analytics_filter) or "None")} · '
        f'Severity: {escape(", ".join(severity_filter) or "None")} · {start_date} to {end_date} (SGT)</div>',
        unsafe_allow_html=True,
    )
    pg.run()


if __name__ == "__main__":
    main()
