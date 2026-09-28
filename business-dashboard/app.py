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

import logging
import os
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential

from evidence import EvidenceStore, EvidenceUnavailable

logger = logging.getLogger("dashboard")

# Categorical slots from the platform's validated dark-mode palette, in fixed
# hue order (blue, orange, aqua, yellow, violet) -- never cycled. Green/red are
# reserved for the severity status palette below, so they're skipped here to
# avoid a categorical series impersonating a status color.
SERIES_COLORS = ["#3987e5", "#d95926", "#199e70", "#c98500", "#9085e9"]
# Severity is a state, not an identity, so it takes the fixed dark-surface
# status palette instead of the categorical one.
SEVERITY_COLORS = {"low": "#0ca30c", "medium": "#fab219", "high": "#d03b3b"}
SEVERITY_ORDER = ["low", "medium", "high"]

DATE_RANGE_PRESETS = {
    "Last 7 days": 7,
    "Last 30 days": 30,
    "Last 3 months": 90,
    "Last 6 months": 182,
    "Last year": 365,
}

PAGE_BG = "#0d0d0d"
SURFACE = "#1a1a19"
GRIDLINE = "#2c2c2a"
INK_SECONDARY = "#c3c2b7"
INK_MUTED = "#898781"


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


@st.cache_data(ttl=60)
def load_incidents() -> pd.DataFrame:
    container = get_container()
    items = list(
        container.query_items(
            query="SELECT * FROM c",
            enable_cross_partition_query=True,
        )
    )
    if not items:
        return pd.DataFrame(
            columns=[
                "event_id", "tenant_id", "site_id", "analytics_type",
                "event_type", "severity", "timestamp",
            ]
        )

    df = pd.DataFrame(items)
    df["timestamp"] = pd.to_datetime(df["timestamp"], format="ISO8601", utc=True)
    return df


@st.cache_resource
def get_evidence_store() -> EvidenceStore | None:
    return EvidenceStore.from_env()


@st.cache_data(ttl=600, show_spinner=False)
def load_evidence(evidence_uri: str) -> bytes | None:
    """Fetch one evidence image, or None when it cannot be shown.

    Cached per URI because Streamlit re-runs this script on every widget
    interaction, and an image that is already on the page has not changed.
    """
    store = get_evidence_store()

    if store is None:
        return None

    try:
        return store.fetch(evidence_uri).data
    except EvidenceUnavailable as exc:
        logger.warning("evidence not shown: %s", exc)
        return None


def inject_css() -> None:
    st.markdown(
        f"""
        <style>
        .block-container {{ padding-top: 2rem; max-width: 1280px; }}

        .kpi-card {{
            background: {SURFACE}; border: 1px solid rgba(255,255,255,0.10);
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
            background: {SURFACE}; border: 1px solid rgba(255,255,255,0.10);
            border-radius: 12px; padding: 18px 18px 6px;
        }}
        .panel h3 {{ margin: 0 0 16px; font-size: 15px; }}

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
    <div class="kpi-card">
      <div class="kpi-top">
        <div class="kpi-icon" style="background:{color}26; color:{color}">{icon}</div>
        {trend_html}
      </div>
      <div class="kpi-label">{label}</div>
      <div class="kpi-value">{value}</div>
    </div>
    """


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
            f'<div class="bar-row"><span class="name">{name}</span>'
            f'<div class="bar-track"><div class="bar-fill" style="width:{width}%; background:{color}"></div></div>'
            f'<span class="count">{int(count)}</span></div>'
        )
    return f'<div class="panel"><h3>{title}</h3>{rows}</div>'


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


def recent_incidents_panel(filtered: pd.DataFrame, limit: int = 8) -> str:
    rows = ""
    for _, row in filtered.sort_values("timestamp", ascending=False).head(limit).iterrows():
        color = SEVERITY_COLORS.get(row["severity"], INK_MUTED)
        rows += (
            '<div class="incident-row">'
            f'<span class="sev-dot" style="background:{color}"></span>'
            '<div class="desc">'
            f'<span class="t">{row["analytics_type"]} — {row["event_type"]}</span>'
            f'<span class="m">{row["tenant_id"]} · {row["site_id"]}</span>'
            "</div>"
            f'<span class="ago">{relative_time(row["timestamp"])}</span>'
            "</div>"
        )
    if not rows:
        rows = '<p style="color:#898781; font-size:13px;">No incidents in range.</p>'
    return f'<div class="panel"><h3>Recent incidents</h3>{rows}</div>'


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
        f'<div><div class="t">{title}</div><div class="d">{detail}</div></div></div>'
        for color, title, detail in insights
    )
    if not rows:
        rows = '<p style="color:#898781; font-size:13px;">Not enough data for insights.</p>'
    return f'<div class="panel"><h3>Key insights</h3>{rows}</div>'


def render_overview(filtered: pd.DataFrame, previous: pd.DataFrame) -> None:
    total = len(filtered)
    high = int((filtered["severity"] == "high").sum())
    medium = int((filtered["severity"] == "medium").sum())
    sites = filtered["site_id"].nunique()
    tenants = filtered["tenant_id"].nunique()
    types = filtered["analytics_type"].nunique()

    prev_total = len(previous)
    prev_high = int((previous["severity"] == "high").sum())
    prev_medium = int((previous["severity"] == "medium").sum())
    prev_sites = previous["site_id"].nunique()
    prev_tenants = previous["tenant_id"].nunique()
    prev_types = previous["analytics_type"].nunique()

    cards = [
        ("Total incidents", total, prev_total, "◆", SERIES_COLORS[0]),
        ("High severity", high, prev_high, "▲", SEVERITY_COLORS["high"]),
        ("Medium severity", medium, prev_medium, "●", SEVERITY_COLORS["medium"]),
        ("Sites affected", sites, prev_sites, "◈", SERIES_COLORS[2]),
        ("Tenants affected", tenants, prev_tenants, "⬡", SERIES_COLORS[4]),
        ("Analytics types", types, prev_types, "◷", SERIES_COLORS[1]),
    ]
    cols = st.columns(len(cards))
    for col, (label, value, prev_value, icon, color) in zip(cols, cards):
        with col:
            st.markdown(
                kpi_card(label, str(value), icon, color, pct_change(value, prev_value)),
                unsafe_allow_html=True,
            )

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
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

    st.write("")

    lower_left, lower_right = st.columns([1.3, 1])
    with lower_left:
        st.markdown(recent_incidents_panel(filtered), unsafe_allow_html=True)
    with lower_right:
        st.markdown(key_insights_panel(filtered, previous), unsafe_allow_html=True)


def render_all_incidents(filtered: pd.DataFrame) -> None:
    st.subheader("All incidents")
    st.caption(f"{len(filtered)} incidents match the current filters.")
    st.dataframe(
        filtered.sort_values("timestamp", ascending=False),
        use_container_width=True,
        hide_index=True,
    )


def render_evidence(filtered: pd.DataFrame, limit: int = 24) -> None:
    st.subheader("Evidence")

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

    with_evidence = filtered[filtered["evidence_uri"].apply(store.describes_evidence)]

    if with_evidence.empty:
        st.info(
            "No incident in the current filters references an image in "
            f"`{store.container}`. Incidents created through "
            "`/api/v1/simulate` carry placeholder paths rather than real blob "
            "references, so they appear here without an image."
        )
        return

    newest = with_evidence.sort_values("timestamp", ascending=False).head(limit)
    st.caption(
        f"{len(with_evidence)} of {len(filtered)} incidents in range reference "
        f"an image." + (f" Showing the {limit} most recent." if len(with_evidence) > limit else "")
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
                    f"{row['severity']} · {relative_time(row['timestamp'])}"
                ),
                width="stretch",
            )


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

    return selected


def main() -> None:
    st.set_page_config(page_title="Incident Business Dashboard", layout="wide")
    inject_css()

    df = load_incidents()
    if df.empty:
        st.title("Scalable Incident Platform — Business Dashboard")
        st.info("No incidents found in the store yet.")
        st.stop()

    with st.sidebar:
        st.header("Filters")

        tenants = sorted(df["tenant_id"].unique())
        tenant_filter = checkbox_filter("Tenant", tenants, "tenant")

        analytics_types = sorted(df["analytics_type"].unique())
        analytics_filter = checkbox_filter("Analytics type", analytics_types, "analytics")

        severities = [s for s in SEVERITY_ORDER if s in df["severity"].unique()]
        severity_filter = checkbox_filter("Severity", severities, "severity")

        min_date = df["timestamp"].min().date()
        max_date = df["timestamp"].max().date()

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
    pg = st.navigation([overview_page, incidents_page, evidence_page])

    st.title("Scalable Incident Platform — Business Dashboard")
    pg.run()


main()
