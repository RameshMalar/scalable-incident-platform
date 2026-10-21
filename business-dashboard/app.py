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

import json
import time
from html import escape
from urllib.parse import urlparse, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import URLError

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
from dashboard_security import AccessDenied, OperatorContext, OperatorVerifier, access_token, id_token
from account_setup import render_account_setup
from access_api import AccessAPIUnavailable, current_grant
from access_roles import InvalidAccess
from admin_access import render_admin_access
import local_auth

logger = logging.getLogger("dashboard")

# Consistent categorical accents on navy surfaces. Severity always includes text.
SERIES_COLORS = ["#38bdf8", "#818cf8", "#22d3ee", "#fbbf24", "#c084fc"]
# Severity is a state, not an identity, so it takes the fixed dark-surface
# status palette instead of the categorical one.
SEVERITY_COLORS = {"low": "#67e8f9", "medium": "#fbbf24", "high": "#fda4af", "critical": "#f0abfc"}
SEVERITY_ORDER = ["critical", "high", "medium", "low"]

DATE_RANGE_PRESETS = {
    "Last 7 days": 7,
    "Last 30 days": 30,
    "Last 3 months": 90,
    "Last 6 months": 182,
    "Last year": 365,
}

PAGE_BG = "#081222"
SURFACE = "#111f35"
GRIDLINE = "#263b56"
# Matches .streamlit/config.toml textColor. The custom panels always draw a
# dark background, so they also set their own text colour rather than
# inheriting it from whatever Streamlit theme is active.
INK_PRIMARY = "#edf5ff"
INK_SECONDARY = "#d0dceb"
INK_MUTED = "#9bafc7"


@st.cache_resource
def get_container():
    endpoint = os.environ.get("COSMOS_ENDPOINT")
    if not endpoint:
        st.error(
            "COSMOS_ENDPOINT is not set, e.g. "
            "https://incident-cosmos-xxxx.documents.azure.com:443/"
        )
        st.stop()

    database_name = os.environ.get("COSMOS_DATABASE", "incidents")
    container_name = os.environ.get("COSMOS_CONTAINER", "incidents")

    credential = DefaultAzureCredential()
    client = CosmosClient(endpoint, credential=credential)
    return client.get_database_client(database_name).get_container_client(container_name)


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


def local_operator_context(username: str) -> OperatorContext:
    """Read-only access to the approved Admin's platform tenants, nothing more."""
    policy = json.loads((Path(__file__).with_name("access-policy.json")).read_text(encoding="utf-8"))
    tenants = sorted({tenant for entry in policy["users"].values() for tenant in entry["tenants"]})
    return OperatorContext(f"local:{username}", policy["directory_id"], tuple(tenants),
                           frozenset({"Dashboard.Read"}), int(time.time()) + local_auth.IDLE_SECONDS, "SOC Viewer")


def delegated_api_token():
    require_operator()
    return access_token(st.context.headers, operator_tokens())


def require_operator():
    if os.environ.get("DASHBOARD_AUTH_MODE", "oidc") == "local":
        # Draws the sign-in form and stops the page until signed in.
        return local_operator_context(local_auth.require_signed_in(st.context.headers))
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
        st.title("Smart Incident Monitoring Platform")
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
    if (not isinstance(evidence_uri, str) or "evidence_uri" not in records
            or not records["evidence_uri"].eq(evidence_uri).any()):
        return None
    return (_load_preview if thumbnail else _load_original)(context.tenants, evidence_uri)


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
        .block-container {{ padding-top: 2rem; max-width: 1600px; }}
        .stApp {{ background: {PAGE_BG}; }}
        h1, h2, h3 {{ letter-spacing: -0.02em; }}
        .soc-kpis {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:16px; }}
        .soc-kpis .kpi-card {{ border-top:3px solid var(--accent); min-height:145px; }}
        .kpi-value {{ overflow-wrap:anywhere; }}
        @media(max-width:1100px) {{ .soc-kpis {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} }}
        @media(max-width:600px) {{ .soc-kpis {{ grid-template-columns:1fr; }} }}

        .kpi-card {{
            background: {SURFACE}; color: {INK_PRIMARY}; border: 1px solid rgba(255,255,255,0.10);
            border-radius: 12px; padding: 16px;
        }}
        .kpi-top {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; }}
        .kpi-icon {{
            width: 34px; height: 34px; border-radius: 9px;
            display: flex; align-items: center; justify-content: center; font-size: 16px;
        }}
        .kpi-trend {{ font-size: 12px; font-variant-numeric: tabular-nums; }}
        .kpi-trend.up {{ color: #0ca30c; }}
        .kpi-trend.down {{ color: #d03b3b; }}
        .kpi-trend.flat {{ color: {INK_MUTED}; }}
        .kpi-label {{ font-size: 12.5px; color: {INK_SECONDARY}; margin-bottom: 4px; }}
        .kpi-value {{ font-size: 26px; font-weight: 700; font-variant-numeric: tabular-nums; }}

        .panel {{
            background: {SURFACE}; color: {INK_PRIMARY}; border: 1px solid rgba(255,255,255,0.10);
            border-radius: 12px; padding: 18px 18px 6px;
        }}
        .panel h3 {{ margin: 0 0 16px; font-size: 15px; color: {INK_PRIMARY}; }}

        .bar-row {{ display: flex; align-items: center; gap: 10px; font-size: 13.5px; margin-bottom: 12px; }}
        .bar-row .name {{ width: 110px; color: {INK_SECONDARY}; flex-shrink: 0; }}
        .bar-track {{ flex: 1; height: 8px; background: {GRIDLINE}; border-radius: 4px; overflow: hidden; }}
        .bar-fill {{ height: 100%; border-radius: 4px; }}
        .bar-row .count {{ width: 28px; text-align: right; font-variant-numeric: tabular-nums; }}

        .incident-row {{ display: flex; align-items: center; gap: 10px; padding: 9px 4px; border-radius: 8px; font-size: 13.5px; }}
        .incident-row .sev-dot {{ width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }}
        .incident-row .desc {{ flex: 1; }}
        .incident-row .desc .t {{ display: block; }}
        .incident-row .desc .m {{ color: {INK_MUTED}; font-size: 12px; }}
        .incident-row .ago {{ color: {INK_MUTED}; font-size: 12px; white-space: nowrap; }}

        .insight-row {{ display: flex; gap: 10px; padding: 10px 0; border-bottom: 1px solid {GRIDLINE}; }}
        .insight-row:last-child {{ border-bottom: none; }}
        .insight-dot {{ width: 8px; height: 8px; border-radius: 50%; margin-top: 5px; flex-shrink: 0; }}
        .insight-row .t {{ font-size: 13.5px; margin-bottom: 2px; }}
        .insight-row .d {{ font-size: 12.5px; color: {INK_MUTED}; }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def kpi_card(label: str, value: str, icon: str, color: str, delta: float | None) -> str:
    if delta is None:
        trend_html = '<span class="kpi-trend flat">—</span>'
    elif delta == 0:
        trend_html = '<span class="kpi-trend flat">flat</span>'
    else:
        cls = "up" if delta > 0 else "down"
        arrow = "↑" if delta > 0 else "↓"
        trend_html = f'<span class="kpi-trend {cls}">{arrow} {abs(delta):.0f}%</span>'

    return f"""
    <div class="kpi-card" style="--accent:{color}">
      <div class="kpi-top">
        <div class="kpi-icon" style="background:{color}26; color:{color}">{icon}</div>
        {trend_html}
      </div>
      <div class="kpi-label">{escape(label)}</div>
      <div class="kpi-value">{escape(value)}</div>
    </div>
    """.strip()


def pct_change(current: int, previous: int) -> float | None:
    if previous == 0:
        return None
    return (current - previous) / previous * 100


def bar_panel(title: str, counts: pd.Series, color_map: dict) -> str:
    max_count = counts.max() if len(counts) else 1
    rows = ""
    for name, count in counts.items():
        width = int(count / max_count * 100) if max_count else 0
        color = color_map.get(name, SERIES_COLORS[0])
        rows += (
            f'<div class="bar-row"><span class="name">{escape(str(name))}</span>'
            f'<div class="bar-track"><div class="bar-fill" style="width:{width}%; background:{color}"></div></div>'
            f'<span class="count">{int(count)}</span></div>'
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


def render_latest_incidents(filtered: pd.DataFrame, limit: int = 8) -> None:
    require_operator()
    st.subheader("Latest persisted incidents")
    st.caption("Newest event timestamps first · Cosmos DB records only · all times UTC")
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
                severity = str(row["severity"])
                color = SEVERITY_COLORS.get(severity, INK_SECONDARY)
                st.markdown(f'<span style="color:{color};font-weight:700">{escape(severity.upper())}</span>', unsafe_allow_html=True)
                st.text(f"{row['event_type']} · {row['analytics_type']}")
                st.caption(f"Tenant {row['tenant_id']} · Site {row['site_id']} · Camera/source {row.get('source_id', 'Unavailable')}")
                st.caption(row["timestamp"].strftime("%Y-%m-%d %H:%M:%S UTC"))
                purpose = row.get("event_purpose", "incident")
                st.caption(f"TEST · {purpose}" if purpose in ("e2e_test", "load_test") else "Purpose: incident")
                st.text(f"Event ID: {row['event_id']}")


def key_insights_panel(filtered: pd.DataFrame, previous: pd.DataFrame) -> str:
    insights = []

    total = len(filtered)
    high = int((filtered["severity"] == "high").sum())
    if total:
        share = high / total * 100
        insights.append((
            "#d03b3b",
            "High-severity share",
            f"{high} of {total} incidents ({share:.0f}%) in the selected range.",
        ))

    if total and filtered["tenant_id"].nunique():
        top_tenant = filtered["tenant_id"].value_counts().idxmax()
        top_count = filtered["tenant_id"].value_counts().max()
        insights.append((
            SERIES_COLORS[0],
            f"{top_tenant} is the top contributor",
            f"{top_count} incidents, {top_count / total * 100:.0f}% of total volume.",
        ))

    prev_total = len(previous)
    change = pct_change(total, prev_total)
    if change is not None:
        direction = "up" if change > 0 else "down" if change < 0 else "flat"
        insights.append((
            "#199e70",
            f"Volume trending {direction}",
            f"{change:+.0f}% vs. the previous period of equal length.",
        ))

    rows = "".join(
        f'<div class="insight-row"><span class="insight-dot" style="background:{color}"></span>'
        f'<div><div class="t">{escape(title)}</div><div class="d">{escape(detail)}</div></div></div>'
        for color, title, detail in insights
    )
    if not rows:
        rows = '<p style="color:#9bafc7; font-size:13px;">Not enough data for insights.</p>'
    return f'<div class="panel"><h3>Key insights</h3>{rows}</div>'


def render_overview(filtered: pd.DataFrame, previous: pd.DataFrame) -> None:
    require_operator()
    total = len(filtered)
    high = int((filtered["severity"] == "high").sum())
    medium = int((filtered["severity"] == "medium").sum())
    sites = filtered["site_id"].nunique()
    tenants = filtered["tenant_id"].nunique()
    types = filtered["analytics_type"].nunique()

    prev_total = len(previous)
    prev_high = int((previous["severity"] == "high").sum())

    critical = int((filtered["severity"] == "critical").sum())
    latest = filtered["timestamp"].max()
    cards = [
        ("Total incidents", str(total), "◆", SERIES_COLORS[0], pct_change(total, prev_total)),
        ("Critical incidents", str(critical), "!", SEVERITY_COLORS["critical"], None),
        ("High-severity incidents", str(high), "▲", SEVERITY_COLORS["high"], pct_change(high, prev_high)),
        ("Latest incident · UTC", latest.strftime("%d %b %Y %H:%M:%S") if pd.notna(latest) else "No incidents", "◷", SERIES_COLORS[4], None),
    ]
    st.markdown('<div class="soc-kpis">' + ''.join(kpi_card(*card) for card in cards) + '</div>', unsafe_allow_html=True)
    secondary_cards = [
        ("Medium severity", medium, int((previous["severity"] == "medium").sum()), SEVERITY_COLORS["medium"]),
        ("Sites affected", sites, previous["site_id"].nunique(), SERIES_COLORS[2]),
        ("Tenants affected", tenants, previous["tenant_id"].nunique(), SERIES_COLORS[4]),
        ("Analytics types", types, previous["analytics_type"].nunique(), SERIES_COLORS[1]),
    ]
    st.markdown('<div class="soc-kpis">' + ''.join(kpi_card(label, str(value), "·", color, pct_change(value, prev)) for label, value, prev, color in secondary_cards) + '</div>', unsafe_allow_html=True)
    st.caption(f"{medium} medium severity · {sites} sites · {tenants} tenants · {types} analytics types. Counts include all persisted event purposes within current filters.")

    st.write("")

    if filtered.empty:
        st.warning("No incidents match the current filters.")
        return

    left, right = st.columns(2)
    with left:
        by_type = filtered["analytics_type"].value_counts()
        color_map = dict(zip(by_type.index, SERIES_COLORS * (len(by_type) // len(SERIES_COLORS) + 1)))
        st.markdown(bar_panel("Incidents by analytics type", by_type, color_map), unsafe_allow_html=True)
    with right:
        by_severity = filtered["severity"].value_counts().reindex(SEVERITY_ORDER).dropna()
        st.markdown(bar_panel("Severity distribution", by_severity, SEVERITY_COLORS), unsafe_allow_html=True)

    st.write("")

    daily = (
        filtered.set_index("timestamp")
        .resample("D")
        .size()
        .reset_index(name="count")
    )
    fig = go.Figure(
        go.Scatter(
            x=daily["timestamp"], y=daily["count"],
            mode="lines", line=dict(color=SERIES_COLORS[0], width=2, shape="spline"),
            fill="tozeroy", fillcolor="rgba(57,135,229,0.12)",
        )
    )
    fig.update_layout(
        title="Incidents per day",
        paper_bgcolor=SURFACE, plot_bgcolor=SURFACE,
        font_color=INK_SECONDARY,
        margin=dict(l=10, r=10, t=40, b=10), height=220,
        xaxis=dict(gridcolor=GRIDLINE, showgrid=False),
        yaxis=dict(gridcolor=GRIDLINE),
    )
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})

    st.write("")

    lower_left, lower_right = st.columns([1.3, 1])
    with lower_left:
        render_latest_incidents(filtered)
    with lower_right:
        st.markdown(key_insights_panel(filtered, previous), unsafe_allow_html=True)


def render_all_incidents(filtered: pd.DataFrame) -> None:
    require_operator()
    st.subheader("All incidents")
    st.caption(f"{len(filtered)} incidents match the current filters.")
    st.dataframe(
        ordered_incidents(filtered),
        width="stretch",
        hide_index=True,
    )


@st.dialog("Evidence", width="large")
def _evidence_detail_dialog(row: pd.Series, image: bytes) -> None:
    require_operator()
    full = load_evidence(row["evidence_uri"], thumbnail=False)
    st.image(full or image, width="stretch")
    st.markdown(f"**{row['event_id']}**")
    st.caption(
        f"{row['analytics_type']} · {row['severity']} · "
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
                    f"{row['event_id']} · {row['analytics_type']} · "
                    f"{row['severity']} · {relative_time(row['timestamp'])} · "
                    + ("TEST" if row.get("event_purpose") in ("e2e_test", "load_test") else "Incident")
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
    category = st.selectbox("Analytics category", ["All"] + categories)
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


SIMULATOR_TYPES = ("intrusion", "ppe", "crowd", "fire", "smoke")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def submit_single_event(base_url: str, analytics_type: str, token: str, authorized_tenants: tuple[str, ...], opener=None) -> str:
    """One POST, no retries or redirects. Acceptance is never persistence."""
    parsed = urlparse(base_url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise ValueError("Configure an HTTPS ingestion origin without a path or credentials.")
    if analytics_type not in SIMULATOR_TYPES:
        raise ValueError("Unsupported simulator analytics type.")
    if not token or "\r" in token or "\n" in token:
        raise ValueError("A delegated API token is required")
    transport = opener or build_opener(NoRedirect())
    if len(authorized_tenants) != 1:
        raise ValueError("Select exactly one authorized tenant")
    query = "?" + urlencode({"tenant_id": authorized_tenants[0]})
    capability_request = Request(base_url.rstrip("/") + "/api/v1/simulation/capabilities" + query, headers={"Authorization": "Bearer " + token})
    with transport.open(capability_request, timeout=10) as response:
        capability = json.loads(response.read(65536))
        if (response.status != 200 or not isinstance(capability, dict)
                or capability.get("authorization") != "entra-user-v1"
                or capability.get("tenant_id") not in authorized_tenants
                or capability.get("event_purpose") != "e2e_test"):
            raise ValueError("Backend authorization is not verified")
    request = Request(base_url.rstrip("/") + "/api/v1/simulate/" + analytics_type + query, data=b"", method="POST", headers={"Authorization": "Bearer " + token})
    # Never retry an ambiguous timeout: the service might already have published.
    with transport.open(request, timeout=10) as response:
        if response.status not in (200, 202):
            raise ValueError("Ingestion did not accept the request.")
        payload = json.loads(response.read(65536))
    if not isinstance(payload, dict) or payload.get("status") != "accepted" or not isinstance(payload.get("event_id"), str) or not payload["event_id"]:
        raise ValueError("Ingestion did not return an accepted event ID.")
    return payload["event_id"]


def render_simulator() -> None:
    context = require_operator()
    if not context.can_simulate:
        st.error("Simulation permission is required.")
        return
    st.subheader("Controlled Event Simulator")
    st.caption("TEST event · single submission · existing API event-purpose routing applies.")
    enabled = os.environ.get("ENABLE_EVENT_SIMULATOR", "").lower() == "true"
    verified = os.environ.get("SIMULATOR_AUTH_VERIFIED", "").lower() == "true"
    endpoint = os.environ.get("SIMULATOR_INGESTION_ORIGIN", "")
    if not enabled or not verified or not endpoint:
        st.info("Simulation remains disabled until dashboard and backend authorization have been verified.")
        return
    try:
        token = delegated_api_token()
    except AccessDenied:
        st.error("A delegated API token is unavailable. Simulation is disabled.")
        return
    st.warning("A confirmed test may send real notifications to eligible subscribers. The existing API determines severity and evidence for the selected type.")
    with st.form("single_event", clear_on_submit=True):
        selected_tenant = st.selectbox("Authorized test tenant", context.tenants)
        analytics = st.selectbox("Demo analytics", SIMULATOR_TYPES)
        acknowledged = st.checkbox("Submit exactly one test event and allow its eligible notifications.")
        submitted = st.form_submit_button("Confirm one test event")
    if submitted:
        now = time.monotonic()
        pending = st.session_state.get("simulator_pending")
        if pending:
            st.warning("Resolve the previous request before another submission.")
        elif not acknowledged:
            st.error("Explicit confirmation is required.")
        elif now - st.session_state.get("simulator_last_attempt", -60) < 60:
            st.warning("Wait 60 seconds between attempts.")
        else:
            st.session_state["simulator_last_attempt"] = now
            # Retain an ambiguous outcome across reruns; never retry automatically.
            st.session_state["simulator_pending"] = {"event_id": None, "state": "unknown"}
            try:
                event_id = submit_single_event(endpoint, analytics, token, (selected_tenant,))
            except (ValueError, URLError, TimeoutError, OSError):
                st.error("Acceptance is unknown. Check persisted incidents and backend logs before another attempt.")
            else:
                st.session_state["simulator_pending"] = {"event_id": event_id, "state": "accepted"}
    pending = st.session_state.get("simulator_pending")
    if pending:
        event_id = pending["event_id"]
        records = load_incidents()
        if event_id and records["event_id"].eq(event_id).any():
            st.success(f"Confirmed in Cosmos DB: {event_id}")
        elif event_id:
            st.info(f"API accepted {event_id}; Cosmos persistence is pending. Refresh incidents to check.")
        else:
            st.warning("Previous request outcome is unknown; no automatic retry will occur.")
        if st.button("I reviewed the previous request; allow a new test"):
            st.session_state.pop("simulator_pending", None)
            st.rerun()


def main() -> None:
    st.set_page_config(page_title="Incident Business Dashboard", layout="wide")
    inject_css()
    context = require_operator()
    with st.sidebar:
        st.caption("Authenticated operator · authorized tenant scope enforced")
        mode = os.environ.get("DASHBOARD_AUTH_MODE", "oidc")
        if mode == "easyauth":
            st.link_button("Sign out", "/.auth/logout")
        elif mode == "local":
            if st.button("Sign out"):
                local_auth.sign_out()
                st.rerun()
        elif st.button("Sign out"):
            st.logout()

    with st.sidebar:
        refresh = st.selectbox("Refresh interval", ["Manual", "5 minutes", "15 minutes"], index=0)
        st.caption("Tenant snapshots are cached for five minutes. Manual refresh is limited to once per minute per session and never submits events.")
    interval = {"Manual": None, "5 minutes": 300, "15 minutes": 900}[refresh]

    @st.fragment(run_every=interval)
    def refresh_clock():
        require_operator()
        now = time.monotonic()
        last = st.session_state.get("soc_refresh_tick", now)
        st.session_state.setdefault("soc_refresh_tick", now)
        manual = st.button("Refresh incidents")
        if manual and now - st.session_state.get("soc_last_manual_refresh", -60) < 60:
            st.info("Wait one minute between manual data refreshes.")
            return
        if manual:
            st.session_state["soc_last_manual_refresh"] = now
            _load_incidents.clear()
        if manual or (interval and now - last >= interval):
            st.session_state["soc_refresh_tick"] = now
            st.rerun()
    refresh_clock()
    df = load_incidents()

    with st.sidebar:
        st.link_button(
            "Platform Observability",
            "https://incident-grafana.calmmushroom-4789bfc0.malaysiawest.azurecontainerapps.io/d/incident-platform-ops/incident-platform-7c-operations",
        )

        st.header("Filters")

        tenants = sorted(df["tenant_id"].unique())
        tenant_filter = checkbox_filter("Tenant", tenants, "tenant")

        analytics_types = sorted(df["analytics_type"].unique())
        analytics_filter = checkbox_filter("Analytics type", analytics_types, "analytics")

        severities = [s for s in SEVERITY_ORDER if s in df["severity"].unique()] + sorted(set(df["severity"].dropna()) - set(SEVERITY_ORDER))
        severity_filter = checkbox_filter("Severity", severities, "severity")

        min_date = df["timestamp"].min().date() if not df.empty else date.today()
        max_date = df["timestamp"].max().date() if not df.empty else date.today()

        range_preset = st.selectbox(
            "Date range", list(DATE_RANGE_PRESETS) + ["Custom range"], index=1
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
            & frame["analytics_type"].isin(analytics_filter)
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

    overview_page = st.Page(
        lambda: render_overview(filtered, previous),
        title="Overview", icon="📊", default=True, url_path="overview",
    )
    incidents_page = st.Page(
        lambda: render_all_incidents(filtered),
        title="All incidents", icon="📄", url_path="all-incidents",
    )
    evidence_page = st.Page(
        lambda: render_evidence(filtered),
        title="Evidence", icon="🖼️", url_path="evidence",
    )
    simulator_page = st.Page(render_simulator, title="Controlled Event Simulator", icon="🧪", url_path="simulator")
    pages = [overview_page, incidents_page, evidence_page]
    pages.append(st.Page(lambda: render_account_setup(require_operator, delegated_api_token), title="Account Setup", icon="👤", url_path="account-setup"))
    if getattr(context,'can_manage_access',False):
        pages.append(st.Page(lambda: render_admin_access(require_operator, delegated_api_token),title="Admin Access Management",icon="🔐",url_path="admin-access"))
    if context.can_simulate:
        pages.append(simulator_page)
    pg = st.navigation(pages)

    st.title("Security Operations Center")
    st.caption("Scalable Incident Platform · Business Dashboard")
    st.text(f"Active tenants: {', '.join(tenant_filter) or 'None'}")
    st.caption(f"Analytics: {', '.join(analytics_filter) or 'None'} · Severity: {', '.join(severity_filter) or 'None'} · {start_date} to {end_date} (UTC)")
    st.caption(f"Last successful Cosmos refresh: {df.attrs.get('refreshed_at', 'Unavailable')}")
    pg.run()


if __name__ == "__main__":
    main()
