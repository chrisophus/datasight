"""CLI command module."""

import asyncio
import json
import os
import sys
from pathlib import Path

import rich_click as click

from datasight.data_profile import (
    build_dimension_overview,
    find_table_info,
)

from datasight import cli
from datasight.cli_helpers import _epilog


@click.command(
    epilog=_epilog(
        """
        Examples:

            datasight dimensions
            datasight dimensions --table generation_fuel
            datasight dimensions --format json -o dimensions.json
        """
    )
)
@click.option(
    "--project-dir",
    type=click.Path(exists=True),
    default=".",
    help="Project directory containing .env and config files.",
)
@click.option("--table", default=None, help="Inspect dimensions for a specific table.")
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
    help="Write the dimension overview to a file instead of stdout.",
)
def dimensions(project_dir, table, output_format, output_path):
    """Surface likely grouping dimensions and category breakdowns.

    Use this to find text/code columns that are good GROUP BY candidates,
    such as fuel codes, states, sectors, plants, or scenario labels.
    """
    from rich.console import Console

    project_dir = str(Path(project_dir).resolve())
    settings, _ = cli._resolve_settings(project_dir)
    resolved_db_path = cli._resolve_db_path(settings, project_dir)
    if settings.database.mode in ("duckdb", "sqlite") and not os.path.exists(resolved_db_path):
        click.echo(f"Error: Database file not found: {resolved_db_path}", err=True)
        sys.exit(1)

    async def _run_dimensions():
        sql_runner, schema_info = await cli._load_schema_info_for_project(project_dir, settings)
        if table:
            table_info = find_table_info(schema_info, table)
            if table_info is None:
                raise click.ClickException(f"Table not found: {table}")
            schema_info = [table_info]
        return await build_dimension_overview(schema_info, sql_runner.run_sql)

    dimension_data = asyncio.run(_run_dimensions())

    if output_format == "json":
        cli._write_or_print(json.dumps(dimension_data, indent=2), output_path)
        return

    if output_format == "markdown":
        cli._write_or_print(cli._render_dimensions_markdown(dimension_data), output_path)
        return

    console = Console(record=bool(output_path))
    console.print(
        cli._build_metric_table(
            "Dimension Overview",
            [("Tables scanned", str(dimension_data["table_count"]))],
        )
    )
    if dimension_data["dimension_columns"]:
        console.print(
            cli._build_profile_detail_table(
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
                        cli._format_profile_value(item.get("distinct_count")),
                        cli._format_profile_value(item.get("null_rate"), "0"),
                        ", ".join((item.get("sample_values") or [])[:3]) or "none",
                    ]
                    for item in dimension_data["dimension_columns"]
                ],
            )
        )
    if dimension_data["suggested_breakdowns"]:
        console.print(
            cli._build_profile_detail_table(
                "Suggested Breakdowns",
                [("Column", "left"), ("Reason", "left")],
                [
                    [f"{item['table']}.{item['column']}", item["reason"]]
                    for item in dimension_data["suggested_breakdowns"]
                ],
            )
        )
    if dimension_data["join_hints"]:
        console.print(
            cli._build_profile_detail_table(
                "Join Hints", [("Hint", "left")], [[item] for item in dimension_data["join_hints"]]
            )
        )
    if output_path:
        cli._write_or_print(console.export_text(), output_path)
