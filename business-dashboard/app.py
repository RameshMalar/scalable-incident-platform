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

import os
from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential

# Categorical slots from the platform's validated palette (blue, orange, aqua).
SERIES_COLORS = ["#2a78d6", "#eb6834", "#1baf7a"]
# Severity is a state, not an identity, so it takes the fixed status palette
# instead of the categorical one.
SEVERITY_COLORS = {"low": "#0ca30c", "medium": "#fab219", "high": "#d03b3b"}
SEVERITY_ORDER = ["low", "medium", "high"]


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


st.set_page_config(page_title="Incident Business Dashboard", layout="wide")
st.title("Scalable Incident Platform — Business Dashboard")

df = load_incidents()

if df.empty:
    st.info("No incidents found in the store yet.")
    st.stop()

# --- Filters -----------------------------------------------------------
with st.sidebar:
    st.header("Filters")

    tenants = sorted(df["tenant_id"].unique())
    tenant_filter = st.multiselect("Tenant", tenants, default=tenants)

    analytics_types = sorted(df["analytics_type"].unique())
    analytics_filter = st.multiselect(
        "Analytics type", analytics_types, default=analytics_types
    )

    severities = [s for s in SEVERITY_ORDER if s in df["severity"].unique()]
    severity_filter = st.multiselect("Severity", severities, default=severities)

    min_date = df["timestamp"].min().date()
    max_date = df["timestamp"].max().date()
    date_range = st.date_input(
        "Date range",
        value=(max(min_date, max_date - timedelta(days=30)), max_date),
        min_value=min_date,
        max_value=max_date,
    )

start_date, end_date = date_range if len(date_range) == 2 else (min_date, max_date)

filtered = df[
    df["tenant_id"].isin(tenant_filter)
    & df["analytics_type"].isin(analytics_filter)
    & df["severity"].isin(severity_filter)
    & (df["timestamp"].dt.date >= start_date)
    & (df["timestamp"].dt.date <= end_date)
]

# --- KPI row -------------------------------------------------------------
col1, col2, col3, col4 = st.columns(4)
col1.metric("Total incidents", len(filtered))
col2.metric("High severity", int((filtered["severity"] == "high").sum()))
col3.metric("Sites affected", filtered["site_id"].nunique())
col4.metric("Tenants affected", filtered["tenant_id"].nunique())

if filtered.empty:
    st.warning("No incidents match the current filters.")
    st.stop()

# --- Charts ---------------------------------------------------------------
left, right = st.columns(2)

with left:
    by_severity = (
        filtered["severity"].value_counts().reindex(SEVERITY_ORDER).dropna().reset_index()
    )
    by_severity.columns = ["severity", "count"]
    fig = px.bar(
        by_severity,
        x="severity",
        y="count",
        color="severity",
        category_orders={"severity": SEVERITY_ORDER},
        color_discrete_map=SEVERITY_COLORS,
        title="Incidents by severity",
    )
    fig.update_layout(showlegend=False)
    st.plotly_chart(fig, use_container_width=True)

with right:
    by_type = filtered["analytics_type"].value_counts().reset_index()
    by_type.columns = ["analytics_type", "count"]
    fig = px.bar(
        by_type,
        x="analytics_type",
        y="count",
        color="analytics_type",
        color_discrete_sequence=SERIES_COLORS,
        title="Incidents by analytics type",
    )
    fig.update_layout(showlegend=False)
    st.plotly_chart(fig, use_container_width=True)

daily = (
    filtered.set_index("timestamp")
    .resample("D")
    .size()
    .reset_index(name="count")
)
fig = px.line(
    daily,
    x="timestamp",
    y="count",
    title="Incidents per day",
    color_discrete_sequence=[SERIES_COLORS[0]],
)
fig.update_traces(line_width=2)
st.plotly_chart(fig, use_container_width=True)

# --- Raw records -----------------------------------------------------------
st.subheader("Incident records")
st.dataframe(
    filtered.sort_values("timestamp", ascending=False),
    use_container_width=True,
    hide_index=True,
)
