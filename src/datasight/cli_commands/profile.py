"""CLI command module."""

import asyncio
import json
import os
import sys
from pathlib import Path

import rich_click as click

from datasight.data_profile import (
    build_column_profile,
    build_dataset_overview,
    build_table_profile,
    find_column_info,
    find_table_info,
)

from datasight import cli
from datasight.cli_helpers import format_epilog


@click.command(
    epilog=format_epilog(
        """
        Examples:

            datasight profile
            datasight profile --table generation_fuel
            datasight profile --column generation_fuel.net_generation_mwh
            datasight profile --format markdown -o profile.md
        """
    )
)
@click.option(
    "--project-dir",
    type=click.Path(exists=True),
    default=".",
    help="Project directory containing .env and config files.",
)
@click.option("--table", default=None, help="Profile a specific table.")
@click.option(
    "--column",
    default=None,
    help="Profile a specific column as table.column.",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["table", "json", "markdown"]),
    default="table",
    help="Output format (default: table).",
)
@click.option(
    "--output",
    "-o",
    "output_path",
    type=click.Path(),
    default=None,
    help="Write the profile output to a file instead of stdout.",
)
def profile(project_dir, table, column, output_format, output_path):  # noqa: C901
    """Profile your dataset - row counts, date coverage, and column statistics.

    Use this before asking questions to understand table sizes, candidate
    measures, dimensions, null rates, and date ranges.
    """
    from rich.console import Console

    project_dir = str(Path(project_dir).resolve())
    if table and column:
        click.echo("Error: use either --table or --column, not both.", err=True)
        sys.exit(1)

    settings, _ = cli.resolve_settings(project_dir)
    resolved_db_path = cli.resolve_db_path(settings, project_dir)
    if settings.database.mode in ("duckdb", "sqlite") and not os.path.exists(resolved_db_path):
        click.echo(f"Error: Database file not found: {resolved_db_path}", err=True)
        sys.exit(1)

    async def _run_profile():
        sql_runner, schema_info = await cli.load_schema_info_for_project(project_dir, settings)

        if column:
            if "." not in column:
                msg = "--column must be in table.column form."
                raise click.ClickException(msg)
            table_name, column_name = column.split(".", 1)
            table_info = find_table_info(schema_info, table_name)
            if table_info is None:
                msg = f"Table not found: {table_name}"
                raise click.ClickException(msg)
            column_info = find_column_info(table_info, column_name)
            if column_info is None:
                msg = f"Column not found: {column}"
                raise click.ClickException(msg)
            return "column", await build_column_profile(
                table_info, column_info, sql_runner.run_sql
            )

        if table:
            table_info = find_table_info(schema_info, table)
            if table_info is None:
                msg = f"Table not found: {table}"
                raise click.ClickException(msg)
            return "table", await build_table_profile(table_info, sql_runner.run_sql)

        return "dataset", await build_dataset_overview(schema_info, sql_runner.run_sql)

    scope, profile_data = asyncio.run(_run_profile())

    if output_format == "json":
        cli.write_or_print(json.dumps(profile_data, indent=2), output_path)
        return

    if output_format == "markdown":
        cli.write_or_print(cli.render_profile_markdown(scope, profile_data), output_path)
        return

    console = Console(record=bool(output_path))
    if scope == "dataset":
        summary = cli.build_metric_table(
            "Dataset Profile",
            [
                ("Tables", str(profile_data["table_count"])),
                ("Columns", str(profile_data["total_columns"])),
                ("Rows", str(profile_data["total_rows"])),
            ],
        )
        console.print(summary)

        largest = cli.build_profile_detail_table(
            "Largest Tables",
            [("Table", "left"), ("Rows", "right"), ("Columns", "right")],
            [
                [
                    item["name"],
                    f"{item.get('row_count') or 0}",
                    str(item["column_count"]),
                ]
                for item in profile_data["largest_tables"]
            ],
        )
        console.print(largest)
        if profile_data["date_columns"]:
            date_coverage = cli.build_profile_detail_table(
                "Date Coverage",
                [("Column", "left"), ("Min", "left"), ("Max", "left")],
                [
                    [
                        f"{item['table']}.{item['column']}",
                        cli.format_profile_value(item.get("min")),
                        cli.format_profile_value(item.get("max")),
                    ]
                    for item in profile_data["date_columns"]
                ],
            )
            console.print(date_coverage)
        if profile_data["measure_columns"]:
            measures = cli.build_profile_detail_table(
                "Measure Candidates",
                [("Column", "left"), ("Type", "left")],
                [
                    [
                        f"{item['table']}.{item['column']}",
                        cli.format_profile_value(item.get("dtype"), "unknown"),
                    ]
                    for item in profile_data["measure_columns"]
                ],
            )
            console.print(measures)
        if profile_data["dimension_columns"]:
            dimensions = cli.build_profile_detail_table(
                "Dimension Candidates",
                [
                    ("Column", "left"),
                    ("Distinct", "right"),
                    ("Null %", "right"),
                    ("Samples", "left"),
                ],
                [
                    [
                        f"{item['table']}.{item['column']}",
                        cli.format_profile_value(item.get("distinct_count")),
                        cli.format_profile_value(item.get("null_rate"), "0"),
                        ", ".join((item.get("sample_values") or [])[:3]) or "none",
                    ]
                    for item in profile_data["dimension_columns"]
                ],
            )
            console.print(dimensions)
        if output_path:
            cli.write_or_print(console.export_text(), output_path)
        return

    if scope == "table":
        table_summary = cli.build_metric_table(
            f"Table Profile: {profile_data['table']}",
            [
                ("Rows", str(profile_data.get("row_count") or 0)),
                ("Columns", str(profile_data["column_count"])),
            ],
        )
        console.print(table_summary)

        if profile_data["null_columns"]:
            nulls = cli.build_profile_detail_table(
                "Null-heavy Columns",
                [("Column", "left"), ("Nulls", "right"), ("Null %", "right")],
                [
                    [
                        item["column"],
                        str(item["null_count"]),
                        str(item.get("null_rate") or 0),
                    ]
                    for item in profile_data["null_columns"]
                ],
            )
            console.print(nulls)
        if profile_data["date_columns"]:
            dates = cli.build_profile_detail_table(
                "Date Columns",
                [("Column", "left"), ("Min", "left"), ("Max", "left")],
                [
                    [
                        item["column"],
                        cli.format_profile_value(item.get("min")),
                        cli.format_profile_value(item.get("max")),
                    ]
                    for item in profile_data["date_columns"]
                ],
            )
            console.print(dates)
        if profile_data["numeric_columns"]:
            numeric = cli.build_profile_detail_table(
                "Numeric Columns",
                [("Column", "left"), ("Min", "left"), ("Max", "left"), ("Avg", "left")],
                [
                    [
                        item["column"],
                        cli.format_profile_value(item.get("min")),
                        cli.format_profile_value(item.get("max")),
                        cli.format_profile_value(item.get("avg")),
                    ]
                    for item in profile_data["numeric_columns"]
                ],
            )
            console.print(numeric)
        if profile_data["text_columns"]:
            text_dimensions = cli.build_profile_detail_table(
                "Text Dimensions",
                [
                    ("Column", "left"),
                    ("Distinct", "right"),
                    ("Null %", "right"),
                    ("Samples", "left"),
                ],
                [
                    [
                        item["column"],
                        cli.format_profile_value(item.get("distinct_count")),
                        cli.format_profile_value(item.get("null_rate"), "0"),
                        ", ".join((item.get("sample_values") or [])[:3]) or "none",
                    ]
                    for item in profile_data["text_columns"]
                ],
            )
            console.print(text_dimensions)
        if output_path:
            cli.write_or_print(console.export_text(), output_path)
        return

    column_summary = cli.build_metric_table(
        f"Column Profile: {profile_data['table']}.{profile_data['column']}",
        [
            ("Type", str(profile_data.get("dtype") or "unknown")),
            ("Distinct", str(profile_data.get("distinct_count"))),
            ("Nulls", str(profile_data.get("null_count"))),
            ("Null %", str(profile_data.get("null_rate"))),
        ],
    )
    console.print(column_summary)
    if profile_data.get("numeric_stats"):
        stats = profile_data["numeric_stats"]
        console.print(
            cli.build_profile_detail_table(
                "Numeric Stats",
                [("Min", "left"), ("Max", "left"), ("Avg", "left")],
                [
                    [
                        cli.format_profile_value(stats.get("min")),
                        cli.format_profile_value(stats.get("max")),
                        cli.format_profile_value(stats.get("avg")),
                    ]
                ],
            )
        )
    if profile_data.get("date_coverage"):
        stats = profile_data["date_coverage"]
        console.print(
            cli.build_profile_detail_table(
                "Date Coverage",
                [("Min", "left"), ("Max", "left")],
                [
                    [
                        cli.format_profile_value(stats.get("min")),
                        cli.format_profile_value(stats.get("max")),
                    ]
                ],
            )
        )
    if profile_data.get("dimension_stats"):
        stats = profile_data["dimension_stats"]
        console.print(
            cli.build_profile_detail_table(
                "Dimension Stats",
                [("Distinct", "right"), ("Nulls", "right"), ("Samples", "left")],
                [
                    [
                        cli.format_profile_value(stats.get("distinct_count")),
                        cli.format_profile_value(stats.get("null_count")),
                        ", ".join((stats.get("sample_values") or [])[:5]) or "none",
                    ]
                ],
            )
        )
    elif profile_data.get("sample_values"):
        console.print(
            cli.build_profile_detail_table(
                "Sample Values",
                [("Values", "left")],
                [[", ".join(profile_data["sample_values"][:5])]],
            )
        )
    if output_path:
        cli.write_or_print(console.export_text(), output_path)
