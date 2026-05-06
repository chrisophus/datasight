"""CLI command module."""

import shutil
from pathlib import Path

import rich_click as click

from datasight import cli
from datasight.cli_helpers import format_epilog


@click.command(
    epilog=format_epilog(
        """
        Use this when you want to fill in .env, schema_description.md,
        queries.yaml, and time_series.yaml by hand.

        If you already have a DuckDB/SQLite database or CSV/Parquet/Excel
        files and want datasight to inspect them and draft these files, use:

            datasight generate <file>...
        """
    )
)
@click.argument("project_dir", default=".")
@click.option("--overwrite", is_flag=True, help="Overwrite existing files.")
def init(project_dir: str, overwrite: bool):
    """Create blank datasight project template files.

    PROJECT_DIR defaults to the current directory.
    """
    dest = Path(project_dir).resolve()
    dest.mkdir(parents=True, exist_ok=True)

    template_dir = Path(cli.__file__).parent / "templates"

    files = {
        "env.template": ".env",
        "schema_description.md": "schema_description.md",
        "queries.yaml": "queries.yaml",
        "time_series.yaml": "time_series.yaml",
    }

    created = []
    skipped = []

    for src_name, dst_name in files.items():
        src = template_dir / src_name
        dst = dest / dst_name

        if dst.exists() and not overwrite:
            skipped.append(dst_name)
            continue

        shutil.copy2(src, dst)
        created.append(dst_name)

    click.echo(f"Project initialized in {dest}")
    if created:
        click.echo(f"  Created: {', '.join(created)}")
    if skipped:
        click.echo(f"  Skipped (already exist): {', '.join(skipped)}")

    click.echo()
    click.echo("Next steps:")
    click.echo("  1. Store API keys once in ~/.config/datasight/.env:")
    click.echo("     datasight config init")
    click.echo("  2. Edit .env with your database path and (optional) provider/model")
    click.echo("  3. Edit schema_description.md to describe your data")
    click.echo("  4. Edit queries.yaml with example questions")
    click.echo("  5. Or let datasight draft files from data:")
    click.echo("     datasight generate <database-or-files> --overwrite")
    click.echo("  6. Run: datasight run")
