#!/usr/bin/env python3
"""
06_build_dashboard.py

Generate an interactive Plotly dashboard from review time data.
Outputs a single-page static site to docs/index.html for GitHub Pages hosting.

Usage:
    python scripts/06_build_dashboard.py

Prerequisites:
    - Run scripts 01-04 first (or this script will run 04 automatically)
    - pip install plotly jinja2
"""

import subprocess
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from jinja2 import Environment, FileSystemLoader
from scipy.stats import gaussian_kde

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
TEMPLATE_DIR = ROOT / "templates"

# ---------------------------------------------------------------------------
# Plotly theme (shared across all charts)
# ---------------------------------------------------------------------------
COLORS = {
    "primary": "#1a1a2e",
    "accent": "#e94560",
    "secondary": "#0f3460",
    "bg": "#ffffff",
    "grid": "#edf2f7",
}

LAYOUT_DEFAULTS = dict(
    font=dict(family="system-ui, -apple-system, sans-serif", size=13, color="#2d3748"),
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="#fafafa",
    margin=dict(l=20, r=20, t=50, b=40),
    hoverlabel=dict(bgcolor="white", font_size=13),
)

PLOTLY_CONFIG = {
    "responsive": True,
    "displayModeBar": True,
    "modeBarButtonsToRemove": ["lasso2d", "select2d"],
}


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def ensure_summaries():
    """Run script 04 if summary CSVs don't exist."""
    needed = [DATA_DIR / "journal_summary.csv", DATA_DIR / "field_summary.csv"]
    if all(f.exists() for f in needed):
        print("  Summary CSVs already exist")
        return

    print("  Running 04_merge_and_summarize.py ...")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "04_merge_and_summarize.py")],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  ERROR running script 04:\n{result.stderr}")
        sys.exit(1)
    print(result.stdout)


def load_data():
    """Load all required data files."""
    field_summary = pd.read_csv(DATA_DIR / "field_summary.csv")
    journal_summary = pd.read_csv(DATA_DIR / "journal_summary.csv")

    # Load article-level data for histograms
    dfs = []
    for fname in ["pubmed_articles.csv", "crossref_articles.csv"]:
        path = DATA_DIR / fname
        if path.exists():
            dfs.append(pd.read_csv(path))
    articles = pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()

    # Clean article days column
    if not articles.empty:
        articles["days_submission_to_acceptance"] = pd.to_numeric(
            articles["days_submission_to_acceptance"], errors="coerce"
        )
        # Filter bogus values
        articles = articles[
            (articles["days_submission_to_acceptance"] > 0)
            & (articles["days_submission_to_acceptance"] <= 730)
        ].copy()

    return field_summary, journal_summary, articles


# ---------------------------------------------------------------------------
# Chart 1: Field bar chart
# ---------------------------------------------------------------------------
def build_field_bars(field_summary):
    """Horizontal bar chart of median review time by field."""
    df = field_summary.dropna(subset=["field_median_review_days"]).copy()
    df = df.sort_values("field_median_review_days", ascending=True)

    fig = go.Figure()

    # Same color scale as the ridge plot: teal (fast) → red (slow)
    bar_colorscale = [
        [0.0, "rgb(50, 140, 140)"],
        [0.15, "rgb(70, 160, 110)"],
        [0.35, "rgb(180, 180, 60)"],
        [0.55, "rgb(230, 150, 50)"],
        [0.75, "rgb(210, 90, 50)"],
        [1.0, "rgb(170, 40, 45)"],
    ]

    fig.add_trace(
        go.Bar(
            y=df["field"],
            x=df["field_median_review_days"],
            orientation="h",
            marker=dict(
                color=df["field_median_review_days"],
                colorscale=bar_colorscale,
                line=dict(width=0),
            ),
            text=df["field_median_review_days"].round(0).astype(int).astype(str) + "d",
            textposition="outside",
            textfont=dict(size=11),
            customdata=np.stack(
                [
                    df["n_journals_with_data"],
                    df["n_journals_total"],
                    df["coverage_pct"],
                    df["field_p25_review_days"].round(0),
                    df["field_p75_review_days"].round(0),
                ],
                axis=-1,
            ),
            hovertemplate=(
                "<b>%{y}</b><br>"
                "Median: %{x:.0f} days<br>"
                "IQR: %{customdata[3]:.0f}–%{customdata[4]:.0f} days<br>"
                "Journals: %{customdata[0]:.0f}/%{customdata[1]:.0f} (%{customdata[2]:.1f}%% coverage)"
                "<extra></extra>"
            ),
        )
    )

    # Sort toggle buttons
    fig.update_layout(
        **LAYOUT_DEFAULTS,
        height=max(500, len(df) * 28),
        xaxis_title="Median days (submission → acceptance)",
        xaxis=dict(gridcolor=COLORS["grid"]),
        yaxis=dict(automargin=True),
        updatemenus=[
            dict(
                type="buttons",
                direction="left",
                x=1,
                y=1.06,
                xanchor="right",
                buttons=[
                    dict(
                        label="Slowest first",
                        method="relayout",
                        args=[{"yaxis.categoryorder": "total ascending"}],
                    ),
                    dict(
                        label="Fastest first",
                        method="relayout",
                        args=[{"yaxis.categoryorder": "total descending"}],
                    ),
                    dict(
                        label="A → Z",
                        method="relayout",
                        args=[{"yaxis.categoryorder": "category descending"}],
                    ),
                ],
                bgcolor="white",
                bordercolor="#e2e8f0",
                font=dict(size=12),
            )
        ],
    )

    return fig


# ---------------------------------------------------------------------------
# Chart 2: Scatter — review time vs journal ranking
# ---------------------------------------------------------------------------
def build_scatter(journal_summary):
    """Scatter plot of review time vs SJR rank, colored by field."""
    df = journal_summary.dropna(
        subset=["median_days_submission_to_acceptance", "sjr_rank"]
    ).copy()
    df = df[df["n_with_review_time"] >= 5]  # Require some data confidence

    # Cap bubble size range
    size_col = df["n_with_review_time"].clip(upper=200)

    fig = px.scatter(
        df,
        x="sjr_rank",
        y="median_days_submission_to_acceptance",
        color="field",
        size=size_col,
        size_max=18,
        hover_name="journal_name",
        hover_data={
            "field": True,
            "sjr_rank": ":.0f",
            "median_days_submission_to_acceptance": ":.0f",
            "n_with_review_time": True,
        },
        labels={
            "sjr_rank": "SJR Rank (lower = more prestigious)",
            "median_days_submission_to_acceptance": "Median review days",
            "n_with_review_time": "Articles with data",
        },
    )

    fig.update_layout(
        **LAYOUT_DEFAULTS,
        height=600,
        xaxis=dict(gridcolor=COLORS["grid"]),
        yaxis=dict(gridcolor=COLORS["grid"]),
        legend=dict(
            title="Field",
            font_size=11,
            itemsizing="constant",
            bgcolor="rgba(255,255,255,0.9)",
        ),
    )

    # Add field filter dropdown
    fields = sorted(df["field"].unique())
    buttons = [dict(label="All fields", method="update", args=[{"visible": True}])]

    traces_per_field = {}
    for i, trace in enumerate(fig.data):
        name = trace.name
        if name not in traces_per_field:
            traces_per_field[name] = []
        traces_per_field[name].append(i)

    for field in fields:
        visibility = [False] * len(fig.data)
        for i, trace in enumerate(fig.data):
            if trace.name == field:
                visibility[i] = True
        buttons.append(
            dict(label=field[:30], method="update", args=[{"visible": visibility}])
        )

    fig.update_layout(
        updatemenus=[
            dict(
                type="dropdown",
                x=0,
                y=1.15,
                xanchor="left",
                buttons=buttons,
                bgcolor="white",
                bordercolor="#e2e8f0",
                font=dict(size=12),
            )
        ]
    )

    return fig


# ---------------------------------------------------------------------------
# Chart 3: Histogram with field dropdown
# ---------------------------------------------------------------------------
def build_histograms(articles, field_summary):
    """Distribution histograms with a dropdown to select field."""
    if articles.empty:
        return go.Figure().add_annotation(text="No article-level data available")

    valid = articles.dropna(subset=["days_submission_to_acceptance", "field"])

    # Only include fields with enough data
    field_counts = valid.groupby("field").size()
    good_fields = field_counts[field_counts >= 20].index.tolist()
    good_fields.sort()

    if not good_fields:
        return go.Figure().add_annotation(text="Not enough data for histograms")

    # Build median lookup
    medians = field_summary.set_index("field")["field_median_review_days"].to_dict()

    fig = go.Figure()

    # Add one histogram per field (median lines done via shapes)
    for i, field in enumerate(good_fields):
        field_data = valid[valid["field"] == field]["days_submission_to_acceptance"]
        visible = i == 0  # Only first field visible initially

        fig.add_trace(
            go.Histogram(
                x=field_data,
                xbins=dict(size=14),
                marker_color=COLORS["accent"],
                opacity=0.8,
                name=field,
                visible=visible,
                hovertemplate="Days: %{x}<br>Articles: %{y}<extra></extra>",
            )
        )

    # Build dropdown buttons — each field has 1 trace
    n_traces = len(fig.data)
    buttons = []
    for i, field in enumerate(good_fields):
        visibility = [False] * n_traces
        visibility[i] = True
        n_articles = len(valid[valid["field"] == field])
        med = medians.get(field, 0)
        med_val = med if med and not np.isnan(med) else 0

        # Add median line as a shape via relayout
        shapes = []
        if med_val > 0:
            shapes = [
                dict(
                    type="line",
                    x0=med_val, x1=med_val,
                    y0=0, y1=1,
                    yref="paper",
                    line=dict(color=COLORS["primary"], width=2, dash="dash"),
                )
            ]

        buttons.append(
            dict(
                label=field[:35],
                method="update",
                args=[
                    {"visible": visibility},
                    {
                        "title": f"{field} — {n_articles} articles, median {med_val:.0f} days",
                        "shapes": shapes,
                    },
                ],
            )
        )

    first_field = good_fields[0]
    n_first = len(valid[valid["field"] == first_field])
    med_first = medians.get(first_field, 0)
    med_first = med_first if med_first and not np.isnan(med_first) else 0

    # Initial median line shape for the first field
    initial_shapes = []
    if med_first > 0:
        initial_shapes = [
            dict(
                type="line",
                x0=med_first, x1=med_first,
                y0=0, y1=1,
                yref="paper",
                line=dict(color=COLORS["primary"], width=2, dash="dash"),
            )
        ]

    hist_layout = {k: v for k, v in LAYOUT_DEFAULTS.items() if k != "margin"}
    fig.update_layout(
        **hist_layout,
        height=550,
        shapes=initial_shapes,
        margin=dict(l=60, r=20, t=120, b=60),
        title=dict(
            text=f"{first_field} — {n_first} articles, median {med_first:.0f} days",
            x=0.5,
            xanchor="center",
            y=0.92,
        ),
        xaxis_title="Days (submission → acceptance)",
        yaxis_title="Number of articles",
        xaxis=dict(gridcolor=COLORS["grid"]),
        yaxis=dict(gridcolor=COLORS["grid"]),
        showlegend=False,
        bargap=0.05,
        updatemenus=[
            dict(
                type="dropdown",
                x=0,
                y=1.18,
                xanchor="left",
                buttons=buttons,
                bgcolor="white",
                bordercolor="#e2e8f0",
                font=dict(size=12),
                active=0,
            )
        ],
    )

    return fig


# ---------------------------------------------------------------------------
# Chart 4: Ridge plot — overlapping density distributions by field
# ---------------------------------------------------------------------------
def build_ridge_plot(articles, field_summary):
    """Ridge plot with overlapping, transparent density distributions by field."""
    if articles.empty:
        return go.Figure().add_annotation(text="No article-level data available")

    valid = articles.dropna(subset=["days_submission_to_acceptance", "field"])

    # Only include fields with enough data for a meaningful KDE
    field_counts = valid.groupby("field").size()
    good_fields = field_counts[field_counts >= 20].index.tolist()

    if not good_fields:
        return go.Figure().add_annotation(text="Not enough data for ridge plot")

    # Compute medians from article-level data (not field_summary, which may miss some fields)
    medians = {}
    for field in good_fields:
        field_days = valid[valid["field"] == field]["days_submission_to_acceptance"]
        medians[field] = field_days.median()

    # Sort fields by median review time (slowest at top like the bar chart)
    good_fields = sorted(good_fields, key=lambda f: medians.get(f, 0) or 0)

    # KDE evaluation grid
    x_grid = np.linspace(0, 730, 300)

    n_fields = len(good_fields)

    # Overlap: spacing < peak height so ridges overlap vertically
    spacing = 0.45
    peak_scale = 1.3  # peaks extend well above the spacing

    # Color palette: red (slowest) → orange → gold → teal (fastest)
    # Custom scale avoids the washed-out yellows and jarring blues of Turbo
    from plotly.colors import sample_colorscale
    custom_scale = [
        [0.0, "rgb(50, 140, 140)"],    # teal (fastest, bottom)
        [0.15, "rgb(70, 160, 110)"],   # green-teal
        [0.35, "rgb(180, 180, 60)"],   # olive-gold
        [0.55, "rgb(230, 150, 50)"],   # amber
        [0.75, "rgb(210, 90, 50)"],    # burnt orange
        [1.0, "rgb(170, 40, 45)"],     # deep red (slowest, top)
    ]
    hue_values = np.linspace(0, 1, n_fields)
    ridge_colors_rgb = sample_colorscale(custom_scale, hue_values)

    fig = go.Figure()

    # Draw from top to bottom so lower (faster) fields render on top
    for draw_idx, i in enumerate(reversed(range(n_fields))):
        field = good_fields[i]
        field_data = valid[valid["field"] == field]["days_submission_to_acceptance"].values
        y_offset = i * spacing

        try:
            kde = gaussian_kde(field_data, bw_method=0.15)
            density = kde(x_grid)
            # Normalize: tallest peak = peak_scale * spacing
            density = density / density.max() * peak_scale * spacing
        except Exception:
            continue

        color_str = ridge_colors_rgb[i]
        # Parse rgb string to get components
        rgb_vals = color_str.replace("rgb(", "").replace(")", "").split(",")
        r, g, b = [v.strip() for v in rgb_vals]
        line_color = f"rgb({r}, {g}, {b})"
        fill_color = f"rgba({r}, {g}, {b}, 0.35)"

        med = medians.get(field, 0) or 0

        # Baseline trace (invisible bottom edge)
        fig.add_trace(
            go.Scatter(
                x=x_grid,
                y=np.full(len(x_grid), y_offset),
                mode="lines",
                line=dict(width=0),
                showlegend=False,
                hoverinfo="skip",
            )
        )

        # Top curve with fill down to baseline — only the top line is visible
        fig.add_trace(
            go.Scatter(
                x=x_grid,
                y=density + y_offset,
                fill="tonexty",
                fillcolor=fill_color,
                line=dict(color=line_color, width=1.8),
                mode="lines",
                name=field,
                showlegend=False,
                hovertemplate=(
                    f"<b>{field}</b><br>"
                    f"Median: {med:.0f} days<br>"
                    f"Articles: {len(field_data)}"
                    "<extra></extra>"
                ),
            )
        )

        # Median marker line on the ridge
        if med > 0:
            med_density_val = kde(np.array([med]))[0]
            med_density_val = med_density_val / kde(x_grid).max() * peak_scale * spacing
            fig.add_trace(
                go.Scatter(
                    x=[med, med],
                    y=[y_offset, med_density_val + y_offset],
                    mode="lines",
                    line=dict(color="rgba(40, 40, 40, 0.6)", width=1.5, dash="dot"),
                    showlegend=False,
                    hoverinfo="skip",
                )
            )

    # Y-axis tick labels = field names, positioned at mid-height of each ridge
    tickvals = [i * spacing + 0.5 * peak_scale * spacing for i in range(n_fields)]
    ticktext = good_fields

    ridge_layout = {k: v for k, v in LAYOUT_DEFAULTS.items() if k not in ("margin", "plot_bgcolor")}
    fig.update_layout(
        **ridge_layout,
        height=max(700, n_fields * 28),
        margin=dict(l=20, r=20, t=40, b=60),
        xaxis=dict(
            title="Days (submission → acceptance)",
            gridcolor=COLORS["grid"],
            range=[0, 730],
        ),
        yaxis=dict(
            tickvals=tickvals,
            ticktext=ticktext,
            automargin=True,
            showgrid=False,
        ),
        hovermode="closest",
        plot_bgcolor="white",
    )

    return fig


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------
def render_site(field_bars, scatter, histograms, ridge, field_summary, journal_summary, articles):
    """Render the Jinja2 template with chart HTML and stats."""
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))
    template = env.get_template("index.html.j2")

    # Compute summary stats for the header
    fs = field_summary.dropna(subset=["field_median_review_days"])
    slowest = fs.iloc[0] if not fs.empty else None
    fastest = fs.iloc[-1] if not fs.empty else None
    all_medians = fs["field_median_review_days"]

    # Total tracked journals (from the master journal list, not just those with data)
    journal_list_path = DATA_DIR / "journal_list.csv"
    n_tracked = len(pd.read_csv(journal_list_path)) if journal_list_path.exists() else len(journal_summary)
    n_jwd = len(journal_summary[journal_summary["n_with_review_time"] > 0])

    html = template.render(
        chart_field_bars=field_bars.to_html(
            full_html=False, include_plotlyjs=False, config=PLOTLY_CONFIG
        ),
        chart_scatter=scatter.to_html(
            full_html=False, include_plotlyjs=False, config=PLOTLY_CONFIG
        ),
        chart_histograms=histograms.to_html(
            full_html=False, include_plotlyjs=False, config=PLOTLY_CONFIG
        ),
        chart_ridge=ridge.to_html(
            full_html=False, include_plotlyjs=False, config=PLOTLY_CONFIG
        ),
        n_articles=len(articles),
        n_journals=len(journal_summary),
        n_fields=len(field_summary),
        n_journals_with_data=n_jwd,
        n_journals_tracked=n_tracked,
        slowest_field_name=slowest["field"] if slowest is not None else "N/A",
        slowest_field_days=slowest["field_median_review_days"] if slowest is not None else 0,
        fastest_field_name=fastest["field"] if fastest is not None else "N/A",
        fastest_field_days=fastest["field_median_review_days"] if fastest is not None else 0,
        overall_median=all_medians.median() if not all_medians.empty else 0,
        coverage_pct=round(n_jwd / n_tracked * 100, 1),
        build_date=date.today().isoformat(),
    )

    return html


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("Building dashboard...\n")

    print("1. Checking summary data...")
    ensure_summaries()

    print("2. Loading data...")
    field_summary, journal_summary, articles = load_data()
    print(f"   {len(field_summary)} fields, {len(journal_summary)} journals, {len(articles)} articles")

    print("3. Building charts...")
    print("   - Field bar chart")
    fig_bars = build_field_bars(field_summary)
    print("   - Scatter plot")
    fig_scatter = build_scatter(journal_summary)
    print("   - Histograms")
    fig_hist = build_histograms(articles, field_summary)
    print("   - Ridge plot")
    fig_ridge = build_ridge_plot(articles, field_summary)

    print("4. Rendering site...")
    html = render_site(fig_bars, fig_scatter, fig_hist, fig_ridge, field_summary, journal_summary, articles)

    DOCS_DIR.mkdir(exist_ok=True)
    out_path = DOCS_DIR / "index.html"
    out_path.write_text(html, encoding="utf-8")

    size_mb = out_path.stat().st_size / 1024 / 1024
    print(f"\n   Output: {out_path} ({size_mb:.1f} MB)")
    print("   Open in browser to preview, or push to GitHub for Pages hosting.")
    print("\nDone!")


if __name__ == "__main__":
    main()
