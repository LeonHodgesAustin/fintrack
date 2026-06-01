"""
fintrack CLI — Typer-based interface.

Commands:
  fintrack link              Start link server to connect an institution
  fintrack sync              Sync transactions for all linked items
  fintrack report            Monthly spending summary
  fintrack reauth --item ID  Trigger re-authentication for an item
  fintrack items list        Show linked institutions and sync status
"""

from datetime import date
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from .classification import build_chain
from .config import get_settings
from .db import get_connection, get_all_items, migrate

app = typer.Typer(help="FinTrack — personal finance tracker powered by Plaid.")
items_app = typer.Typer(help="Manage linked institutions.")
app.add_typer(items_app, name="items")

console = Console()


def _make_plaid_client():
    from .plaid_client import create_client
    s = get_settings()
    return create_client(s.plaid_client_id, s.plaid_secret, s.plaid_env)


def _make_chain():
    s = get_settings()
    return build_chain(s.get_classifier_chain())


def _open_db():
    s = get_settings()
    migrate(s.db_path)
    return get_connection(s.db_path)


# ── link ──────────────────────────────────────────────────────────────────────

@app.command()
def link(
    port: Optional[int] = typer.Option(None, help="Override LINK_SERVER_PORT"),
):
    """
    Start the Plaid Link server, then open the URL in your browser to connect
    a new institution. The server exits after a successful link.
    """
    import subprocess
    import sys

    s = get_settings()
    p = port or s.link_server_port

    console.print(f"\n[bold green]Starting Link server on http://localhost:{p}[/]")
    console.print("Open that URL in your browser, connect your institution, then Ctrl+C to stop.\n")

    subprocess.run(
        [sys.executable, "-m", "link_server.server"],
        env={
            **__import__("os").environ,
            "LINK_SERVER_PORT": str(p),
        },
    )


# ── sync ──────────────────────────────────────────────────────────────────────

@app.command()
def sync(
    item_id: Optional[str] = typer.Option(None, "--item", "-i", help="Sync a single item by ID"),
):
    """Sync transactions for all linked items (or a single item)."""
    from .sync import sync_all_items, sync_item

    client = _make_plaid_client()
    chain = _make_chain()
    conn = _open_db()

    try:
        if item_id:
            from .db import get_item
            item = get_item(conn, item_id)
            if not item:
                console.print(f"[red]Item '{item_id}' not found.[/]")
                raise typer.Exit(1)
            items_to_sync = {item_id: item}
        else:
            items_to_sync = {i["item_id"]: i for i in get_all_items(conn)}

        if not items_to_sync:
            console.print("[yellow]No linked items found. Run [bold]fintrack link[/] first.[/]")
            raise typer.Exit(0)

        for iid, item in items_to_sync.items():
            console.print(f"Syncing [bold]{item['institution_name']}[/] ({iid[:8]}…)")
            with console.status("  Fetching…"):
                stats = sync_item(client, conn, item, chain)
            console.print(
                f"  [green]+{stats['added']}[/] added  "
                f"[yellow]~{stats['modified']}[/] modified  "
                f"[red]-{stats['removed']}[/] removed"
            )
    finally:
        conn.close()


# ── report ────────────────────────────────────────────────────────────────────

@app.command()
def report(
    month: Optional[str] = typer.Option(
        None, "--month", "-m",
        help="Month to report (YYYY-MM). Defaults to current month.",
    ),
    top: int = typer.Option(10, "--top", "-t", help="Number of top merchants to show"),
):
    """Print a monthly spending summary to stdout."""
    from .reports import monthly_summary, top_merchants, mom_trends

    if month:
        try:
            year, mon = (int(x) for x in month.split("-"))
        except ValueError:
            console.print("[red]--month must be YYYY-MM[/]")
            raise typer.Exit(1)
    else:
        today = date.today()
        year, mon = today.year, today.month

    conn = _open_db()
    try:
        # ── Category breakdown ────────────────────────────────────────────
        summary = monthly_summary(conn, year, mon)
        total = sum(r["total_amount"] for r in summary)

        cat_table = Table(title=f"Spending by Category — {year}-{mon:02d}", show_footer=True)
        cat_table.add_column("Category")
        cat_table.add_column("Amount", justify="right", footer=f"${total:,.2f}")
        cat_table.add_column("Txns", justify="right")
        cat_table.add_column("%", justify="right")

        for row in summary:
            pct = (row["total_amount"] / total * 100) if total else 0
            cat_table.add_row(
                row["category"],
                f"${row['total_amount']:,.2f}",
                str(row["transaction_count"]),
                f"{pct:.1f}%",
            )
        console.print(cat_table)

        # ── Top merchants ─────────────────────────────────────────────────
        merchants = top_merchants(conn, year, mon, limit=top)
        if merchants:
            merch_table = Table(title=f"Top {top} Merchants — {year}-{mon:02d}")
            merch_table.add_column("#")
            merch_table.add_column("Merchant")
            merch_table.add_column("Amount", justify="right")
            merch_table.add_column("Txns", justify="right")

            for i, row in enumerate(merchants, 1):
                merch_table.add_row(
                    str(i),
                    row["merchant"],
                    f"${row['total_amount']:,.2f}",
                    str(row["transaction_count"]),
                )
            console.print(merch_table)

        # ── MoM trend ─────────────────────────────────────────────────────
        trends = mom_trends(conn, months=6)
        if len(trends) > 1:
            trend_table = Table(title="Month-over-Month (last 6 months)")
            trend_table.add_column("Month")
            trend_table.add_column("Total Spend", justify="right")
            trend_table.add_column("Txns", justify="right")
            trend_table.add_column("vs Prior", justify="right")

            for i, row in enumerate(trends):
                delta = ""
                if i > 0:
                    prior = trends[i - 1]["total_amount"]
                    change = row["total_amount"] - prior
                    sign = "+" if change >= 0 else ""
                    color = "red" if change > 0 else "green"
                    delta = f"[{color}]{sign}${change:,.2f}[/]"
                trend_table.add_row(
                    f"{row['year']}-{row['month']:02d}",
                    f"${row['total_amount']:,.2f}",
                    str(row["transaction_count"]),
                    delta,
                )
            console.print(trend_table)

    finally:
        conn.close()


# ── reauth ────────────────────────────────────────────────────────────────────

@app.command()
def reauth(
    item: str = typer.Option(..., "--item", "-i", help="item_id to re-authenticate"),
    port: Optional[int] = typer.Option(None, help="Override LINK_SERVER_PORT"),
):
    """
    Launch the Plaid Link update flow to fix a broken/expired item.
    Useful when the bank requires re-authentication.
    """
    import subprocess
    import sys

    s = get_settings()
    p = port or s.link_server_port

    console.print(f"\n[bold yellow]Starting reauth server for item {item[:8]}…[/]")
    console.print(f"Open http://localhost:{p} in your browser to complete re-authentication.\n")

    subprocess.run(
        [sys.executable, "-m", "link_server.server"],
        env={
            **__import__("os").environ,
            "LINK_SERVER_PORT": str(p),
            "REAUTH_ITEM_ID": item,
        },
    )


# ── items list ────────────────────────────────────────────────────────────────

@items_app.command("list")
def items_list():
    """Show all linked institutions and their last sync time."""
    conn = _open_db()
    try:
        items = get_all_items(conn)
        if not items:
            console.print("[yellow]No linked items. Run [bold]fintrack link[/] to connect an institution.[/]")
            return

        table = Table(title="Linked Institutions")
        table.add_column("Item ID", no_wrap=True)
        table.add_column("Institution")
        table.add_column("Last Synced")
        table.add_column("Has Cursor", justify="center")

        for item in items:
            table.add_row(
                item["item_id"],
                item["institution_name"],
                item["last_synced"] or "never",
                "[green]✓[/]" if item["cursor"] else "[red]✗[/]",
            )
        console.print(table)
    finally:
        conn.close()
