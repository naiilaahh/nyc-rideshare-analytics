"""
Driver Ops Portal: the embedded, operational channel of the NYC rideshare analytics product.

Persona: a marketplace / driver-operations manager at Uber or Lyft who has to decide,
hour by hour, where drivers are needed. Each company is a separate tenant and sees only its
own data, as if the portal were embedded in that company's internal ops tool.

TLC publishes trip records monthly, so "live" is simulated with a replay clock that steps
through real historical hours. Every KPI comes from metrics/kpi.py, the same engine behind
the warehouse SQL and the Tableau calculated fields.

    streamlit run app/streamlit_app.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from metrics.kpi import compute_kpis, fmt, kpi  # noqa: E402

EXPORTS = ROOT / "data" / "exports"
if not (EXPORTS / "hourly_zone_company.parquet").exists() and (ROOT / "data/exports_sample").exists():
    EXPORTS = ROOT / "data" / "exports_sample"
ACTION_LOG = ROOT / "data" / "app_actions" / "action_log.csv"
TILE_KPIS = ["trips", "avg_wait_min", "pickup_within_5min_rate", "driver_earnings_per_engaged_hour"]

st.set_page_config(page_title="Driver Ops Portal", page_icon="🚕", layout="wide")


# ------------------------------------------------------------------ data access
@st.cache_data(show_spinner="Loading hourly zone data ...")
def load() -> tuple[pd.DataFrame, pd.DataFrame]:
    hourly = pd.read_parquet(EXPORTS / "hourly_zone_company.parquet")
    hourly["pickup_hour"] = pd.to_datetime(hourly["pickup_hour"])
    zones = pd.read_parquet(EXPORTS / "dim_zone.parquet")
    hourly = hourly.merge(zones[["zone_id", "zone", "borough", "is_crz"]], left_on="pu_zone_id",
                          right_on="zone_id", how="left")
    return hourly, zones


def tenant_scope(df: pd.DataFrame, tenant: str) -> pd.DataFrame:
    """Row-level security: the ONLY place tenant filtering happens."""
    return df[df["company"] == tenant]


def baseline_hours(ts: datetime) -> list[datetime]:
    return [ts - timedelta(weeks=w) for w in range(1, 5)]


def log_action(tenant: str, ts: datetime, row: pd.Series, action: str) -> None:
    ACTION_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = pd.DataFrame([{
        "logged_at": datetime.now().isoformat(timespec="seconds"), "tenant": tenant,
        "replay_hour": ts.isoformat(), "zone_id": int(row["pu_zone_id"]), "zone": row["zone"],
        "signal": row["signal"], "trips": int(row["trips"]), "baseline_trips": round(row["baseline_trips"], 1),
        "action": action,
    }])
    entry.to_csv(ACTION_LOG, mode="a", header=not ACTION_LOG.exists(), index=False)


# ------------------------------------------------------------------ sidebar
hourly, zones = load()
hours = sorted(hourly["pickup_hour"].unique())
first_scorable = pd.Timestamp(hours[0]) + pd.Timedelta(weeks=4)

st.sidebar.title("Driver Ops Portal")
tenant = st.sidebar.selectbox("Signed in as", ["Uber", "Lyft"], format_func=lambda t: f"{t} · NYC Ops")
data = tenant_scope(hourly, tenant)

if "replay" not in st.session_state:
    st.session_state.replay = max(first_scorable, pd.Timestamp("2025-01-06 08:00")) \
        if pd.Timestamp(hours[-1]) > pd.Timestamp("2025-01-06 08:00") else pd.Timestamp(hours[-1])
st.sidebar.markdown("**Replay clock**")
c1, c2, c3 = st.sidebar.columns(3)
if c1.button("◀ 1h"):
    st.session_state.replay -= pd.Timedelta(hours=1)
if c2.button("1h ▶"):
    st.session_state.replay += pd.Timedelta(hours=1)
if c3.button("+1 day"):
    st.session_state.replay += pd.Timedelta(days=1)
d = st.sidebar.date_input("Date", st.session_state.replay.date(),
                          min_value=first_scorable.date(), max_value=pd.Timestamp(hours[-1]).date())
h = st.sidebar.slider("Hour", 0, 23, st.session_state.replay.hour)
st.session_state.replay = pd.Timestamp(datetime(d.year, d.month, d.day, h))
now = st.session_state.replay

st.sidebar.markdown("**Alert rules**")
z_min = st.sidebar.slider("Min |z-score|", 2.0, 6.0, 3.0, 0.5)
pct_min = st.sidebar.slider("Min change vs baseline", 0.2, 1.5, 0.4, 0.1, format="%.1f")
vol_min = st.sidebar.number_input("Min trips (current or baseline)", 5, 500, 25, 5)
show_drops = st.sidebar.checkbox("Include demand drops", True)
st.sidebar.caption(f"Data: NYC TLC High-Volume FHV trip records · source {EXPORTS.name}/. "
                   "Independent portfolio project, not affiliated with Uber, Lyft or TLC.")

# ------------------------------------------------------------------ header + KPI tiles
st.title(f"NYC Driver Ops · {tenant}")
st.caption(f"Replaying **{now:%A %d %b %Y, %H:00}**. Baseline = same weekday and hour over the previous 4 weeks.")

cur = data[data["pickup_hour"] == now]
base = data[data["pickup_hour"].isin(baseline_hours(now))]
cur_k = compute_kpis(cur, names=TILE_KPIS).iloc[0]
base_k = compute_kpis(base, names=TILE_KPIS).iloc[0]
base_k["trips"] = base_k["trips"] / 4          # weekly average, not a 4-week sum

cols = st.columns(len(TILE_KPIS))
for col, name in zip(cols, TILE_KPIS):
    v, b = cur_k[name], base_k[name]
    delta = None if pd.isna(b) or b == 0 else (v - b) / abs(b)
    inverse = name == "avg_wait_min"
    col.metric(kpi(name)["label"], fmt(name, v), None if delta is None else f"{delta:+.0%} vs baseline",
               delta_color="inverse" if inverse else "normal", help=kpi(name)["definition"])

if cur.empty:
    st.info("No trips recorded for this tenant in this hour.")
    st.stop()

# ------------------------------------------------------------------ alerts
scored = cur.dropna(subset=["baseline_trips"]).copy()
scored = scored[scored["baseline_weeks"] == 4]
scored["signal"] = None
big = (scored[["trips", "baseline_trips"]].max(axis=1) >= vol_min) & (scored["pct_vs_baseline"].abs() >= pct_min)
scored.loc[big & (scored["z_score"] >= z_min), "signal"] = "surge"
if show_drops:
    scored.loc[big & (scored["z_score"] <= -z_min), "signal"] = "drop"
alerts = scored[scored["signal"].notna()].copy()
alerts["avg_wait_min"] = alerts["wait_seconds_sum"] / alerts["wait_trips"].where(alerts["wait_trips"] > 0) / 60
alerts = alerts.sort_values("z_score", key=abs, ascending=False)

st.subheader(f"Zones needing action ({len(alerts)})")
ACTIONS = {"surge": "Send repositioning nudge to nearby drivers",
           "drop": "Check for incident; pause zone incentive"}
if alerts.empty:
    st.success("No zones breach the alert rules this hour.")
for _, r in alerts.head(12).iterrows():
    c = st.columns([3, 1.2, 1.2, 1.4, 3, 1.3, 1])
    icon = "🔺" if r["signal"] == "surge" else "🔻"
    c[0].markdown(f"{icon} **{r['zone']}** · {r['borough']}{' · CRZ' if r['is_crz'] else ''}")
    c[1].metric("Trips", f"{r['trips']:,.0f}", f"{r['pct_vs_baseline']:+.0%}", label_visibility="collapsed")
    c[2].markdown(f"baseline<br>**{r['baseline_trips']:,.0f}**", unsafe_allow_html=True)
    w, bw = r["avg_wait_min"], r["baseline_avg_wait_min"]
    c[3].markdown(f"wait<br>**{w:.1f}** min" + (f" (base {bw:.1f})" if pd.notna(bw) else ""), unsafe_allow_html=True)
    c[4].caption(f"z = {r['z_score']:+.1f}. Suggested: {ACTIONS[r['signal']]}")
    key = f"{tenant}-{now:%Y%m%d%H}-{int(r['pu_zone_id'])}"
    if c[5].button("Take action", key="act-" + key):
        log_action(tenant, now, r, ACTIONS[r["signal"]])
        st.toast(f"Logged: {ACTIONS[r['signal']]} · {r['zone']}")
    if c[6].button("Dismiss", key="dis-" + key):
        log_action(tenant, now, r, "dismissed")
        st.toast(f"Dismissed {r['zone']}")

# ------------------------------------------------------------------ zone drill-down
st.subheader("Zone trend: last 24 hours vs baseline")
zone_options = (alerts["zone"].tolist() or []) + cur.sort_values("trips", ascending=False)["zone"].tolist()
zone_options = list(dict.fromkeys(z for z in zone_options if isinstance(z, str)))
zsel = st.selectbox("Zone", zone_options)
window = data[(data["zone"] == zsel) & (data["pickup_hour"] > now - pd.Timedelta(hours=24)) & (data["pickup_hour"] <= now)]
spine = pd.DataFrame({"pickup_hour": pd.date_range(now - pd.Timedelta(hours=23), now, freq="h")})
window = spine.merge(window[["pickup_hour", "trips", "baseline_trips"]], on="pickup_hour", how="left")
window["trips"] = window["trips"].fillna(0)
fig = go.Figure()
fig.add_scatter(x=window["pickup_hour"], y=window["baseline_trips"], name="Baseline (4-wk avg)",
                line=dict(dash="dot", color="#8a8f98"))
fig.add_scatter(x=window["pickup_hour"], y=window["trips"], name="Trips", line=dict(color="#2463eb", width=3))
fig.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10), legend=dict(orientation="h", y=1.1),
                  yaxis_title="Trips per hour")
st.plotly_chart(fig, use_container_width=True)

# ------------------------------------------------------------------ busiest zones
st.subheader("Busiest zones this hour")
top = compute_kpis(cur, ["zone", "borough"], ["trips", "avg_wait_min", "pickup_within_5min_rate",
                                              "driver_earnings_per_engaged_hour", "avg_fare"])
top = top.sort_values("trips", ascending=False).head(15)
st.dataframe(top.style.format({"trips": "{:,.0f}", "avg_wait_min": "{:.1f}", "pickup_within_5min_rate": "{:.0%}",
                               "driver_earnings_per_engaged_hour": "${:,.2f}", "avg_fare": "${:,.2f}"}),
             hide_index=True, use_container_width=True)

with st.expander("Action log"):
    if ACTION_LOG.exists():
        log = pd.read_csv(ACTION_LOG)
        st.dataframe(log[log["tenant"] == tenant].sort_values("logged_at", ascending=False), hide_index=True)
    else:
        st.caption("No actions logged yet.")

with st.expander("Metric definitions"):
    for name in TILE_KPIS + ["avg_fare"]:
        st.markdown(f"**{kpi(name)['label']}**: {kpi(name)['definition']}")
