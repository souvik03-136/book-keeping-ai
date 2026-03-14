"""
Bokeh chart generation.

Produces a self-contained HTML file for each forecast result so it can be
served statically by the Flask app.
"""

from pathlib import Path

import pandas as pd
from bokeh.layouts import column
from bokeh.models import ColumnDataSource, HoverTool
from bokeh.plotting import figure, output_file, save


def create_bokeh_plots(
    df: pd.DataFrame,
    item_id: str,
    future_months: list,
    predicted_demand,
    upload_folder: Path,
) -> Path:
    """
    Generate side-by-side Bokeh plots for actual vs predicted demand.

    Returns the path to the saved HTML file.
    """
    item_data = df[df["item_id"] == item_id].copy()
    item_data["year_month"] = item_data["transaction_date"].dt.to_period("M")
    actual_agg = item_data.groupby("year_month")["quantity"].sum().reset_index()

    actual_source = ColumnDataSource(data=dict(
        month=actual_agg["year_month"].dt.to_timestamp(),
        quantity=actual_agg["quantity"],
    ))

    predicted_source = ColumnDataSource(data=dict(
        month=pd.to_datetime(future_months),
        quantity=[float(v) for v in predicted_demand],
    ))

    hover = HoverTool(
        tooltips=[("Date", "@month{%b %Y}"), ("Quantity", "@quantity{0,0.0}")],
        formatters={"@month": "datetime"},
    )

    actual_plot = figure(
        title=f"Actual Demand — Item {item_id}",
        x_axis_label="Date",
        y_axis_label="Quantity",
        x_axis_type="datetime",
        width=800,
        height=350,
        background_fill_color="#f8f8f8",
    )
    actual_plot.add_tools(hover)
    actual_plot.line(
        "month", "quantity", source=actual_source,
        line_width=2, color="#1f77b4", legend_label="Actual",
    )
    actual_plot.scatter(
        "month", "quantity", source=actual_source,
        size=7, color="#1f77b4", alpha=0.7,
    )

    predicted_plot = figure(
        title=f"Predicted Demand — Item {item_id}",
        x_axis_label="Date",
        y_axis_label="Quantity",
        x_axis_type="datetime",
        width=800,
        height=350,
        background_fill_color="#f8f8f8",
    )
    predicted_plot.add_tools(hover)
    predicted_plot.line(
        "month", "quantity", source=predicted_source,
        line_width=2, color="#ff7f0e", legend_label="Forecast",
        line_dash="dashed",
    )
    predicted_plot.scatter(
        "month", "quantity", source=predicted_source,
        size=7, color="#ff7f0e", alpha=0.7,
    )

    plot_path = upload_folder / f"forecast_{item_id}.html"
    output_file(str(plot_path), title=f"Forecast — Item {item_id}")
    save(column(actual_plot, predicted_plot))

    return plot_path