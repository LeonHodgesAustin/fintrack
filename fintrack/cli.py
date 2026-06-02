"""
fintrack CLI -- Typer-based interface.

Commands:
  fintrack link              Start link server to connect an institution
  fintrack sync              Sync transactions for all linked items
  fintrack report            Monthly spending summary
  fintrack cashflow          Net cashflow (income vs expenses)
  fintrack review            Interactive review of low-confidence transactions
  fintrack push              Push data to Google Sheets
  fintrack check             Run all alert checks and send via ntfy.sh
  fintrack forecast          Spending forecasts (requires prophet)
  fintrack reauth --item ID  Trigger re-authentication for an item
  fintrack keygen            Generate a Fernet encryption key
  fintrack items list        Show linked institutions and sync status
"""

from datetime import date
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from .classification import build_chain
from .config import get_settings
from .db import configure_encryption, get_connection, get_all_items, migrate

app = typer.Typer(help="FinTrack -- personal finance tracker powered by Plaid.")
items_app = typer.Typer(help="Manage linked institutions.")
app.add_typer(items_app, name="items")

console = Console()

PLAID_CATEGORIES = [
    "FOOD_AND_DRINK", "SHOPPING", "TRANSPORTATION", "TRAVEL",
    "ENTERTAINMENT", "HEALTH", "UTILITIES", "TRANSFER_IN", "TRANSFER_OUT",
    "INCOME", "GENERAL_SERVICES", "GENERAL_MERCHANDISE", "LOAN_PAYMENTS",
    "BANK_FEES", "RENT_AND_UTILITIES", "HOME_IMPROVEMENT", "PERSONAL_CARE",
    "GOVERNMENT_AND_NON_PROFIT", "UNCATEGORIZED",
]


def _make_plaid_client():
    from .plaid_client import create_client
    s = get_settings()
    return create_client(s.plaid_client_id, s.plaid_secret, s.plaid_env)


def _make_chain():
    s = get_settings()
    return build_chain(s.get_classifier_chain())


def _open_db():
    s = get_settings()
    configure_encryption(s.fernet_key)
    migrate(s.db_path)
    return get_connection(s.db_path)


def _load_recurring(conn):
    """Build the merged recurring charge list from auto-detect + manual config."""
    from .recurring import detect_recurring, parse_manual_recurring, merge_recurring
    from .db import get_recurring_excludes
    s = get_settings()
    excludes = get_recurring_excludes(conn) | s.get_recurring_exclude_merchants()
    auto = detect_recurring(
        conn,
        lookback_days=s.recurring_lookback_days,
        min_occurrences=s.recurring_min_occurrences,
        window_days=s.recurring_window_days,
        amount_cv_threshold=s.recurring_amount_tolerance,
        exclude_merchants=excludes,
    )
    manual = parse_manual_recurring(s.recurring_expenses)
    return merge_recurring(auto, manual)


# -- link ----------------------------------------------------------------------

@app.command()
def link(port: Optional[int] = typer.Option(None, help="Override LINK_SERVER_PORT")):
    """Start the Plaid Link server and open the URL in your browser to connect a new institution."""
    import subprocess, sys
    s = get_settings()
    p = port or s.link_server_port
    console.print(f"\n[bold green]Starting Link server on http://localhost:{p}[/]")
    console.print("Open that URL in your browser, connect your institution, then Ctrl+C to stop.\n")
    subprocess.run(
        [sys.executable, "-m", "link_server.server"],
        env={**__import__("os").environ, "LINK_SERVER_PORT": str(p)},
    )


# -- sync ----------------------------------------------------------------------

@app.command()
def sync(item_id: Optional[str] = typer.Option(None, "--item", "-i", help="Sync a single item by ID")):
    """Sync transactions for all linked items (or a single item)."""
    from .sync import ItemAuthError, sync_all_items, sync_item
    client = _make_plaid_client()
    chain  = _make_chain()
    conn   = _open_db()
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
            console.print(f"Syncing [bold]{item['institution_name']}[/] ({iid[:8]}...)")
            try:
                with console.status("  Fetching..."):
                    stats = sync_item(client, conn, item, chain)
                console.print(
                    f"  [green]+{stats['added']}[/] added  "
                    f"[yellow]~{stats['modified']}[/] modified  "
                    f"[red]-{stats['removed']}[/] removed"
                )
            except ItemAuthError as e:
                console.print(f"  [bold red]x Re-authentication required[/]: {e.error_code}")
                console.print(f"    Run: [bold]fintrack reauth --item {iid}[/]")
    finally:
        conn.close()


# -- report --------------------------------------------------------------------

@app.command()
def report(
    month: Optional[str] = typer.Option(None, "--month", "-m", help="Month (YYYY-MM)"),
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
        summary = monthly_summary(conn, year, mon)
        total = sum(r["total_amount"] for r in summary)

        cat_table = Table(title=f"Spending by Category -- {year}-{mon:02d}", show_footer=True)
        cat_table.add_column("Category")
        cat_table.add_column("Amount", justify="right", footer=f"${total:,.2f}")
        cat_table.add_column("Txns", justify="right")
        cat_table.add_column("%", justify="right")
        for row in summary:
            pct = (row["total_amount"] / total * 100) if total else 0
            cat_table.add_row(
                row["category"], f"${row['total_amount']:,.2f}",
                str(row["transaction_count"]), f"{pct:.1f}%",
            )
        console.print(cat_table)

        merchants = top_merchants(conn, year, mon, limit=top)
        if merchants:
            merch_table = Table(title=f"Top {top} Merchants -- {year}-{mon:02d}")
            merch_table.add_column("#")
            merch_table.add_column("Merchant")
            merch_table.add_column("Amount", justify="right")
            merch_table.add_column("Txns", justify="right")
            for i, row in enumerate(merchants, 1):
                merch_table.add_row(str(i), row["merchant"],
                                    f"${row['total_amount']:,.2f}", str(row["transaction_count"]))
            console.print(merch_table)

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
                    f"{row['year']}-{row['month']:02d}", f"${row['total_amount']:,.2f}",
                    str(row["transaction_count"]), delta,
                )
            console.print(trend_table)
    finally:
        conn.close()


# -- cashflow ------------------------------------------------------------------

@app.command()
def cashflow(
    month: Optional[str] = typer.Option(None, "--month", "-m", help="Month (YYYY-MM)"),
    months: int = typer.Option(6, "--trend", help="Months of trend history to show"),
):
    """Net cashflow summary: income vs expenses with internal transfer exclusion."""
    from .cashflow import cashflow_summary, cashflow_trend
    if month:
        try:
            year, mon = (int(x) for x in month.split("-"))
        except ValueError:
            console.print("[red]--month must be YYYY-MM[/]")
            raise typer.Exit(1)
    else:
        today = date.today()
        year, mon = today.year, today.month

    s = get_settings()
    transfer_cats = s.get_cashflow_transfer_categories()
    conn = _open_db()
    try:
        cf = cashflow_summary(conn, year, mon, transfer_categories=transfer_cats)
        label = date(year, mon, 1).strftime("%B %Y")

        console.print(f"\n[bold]Net Cashflow -- {label}[/]\n")
        net_color = "green" if cf["net"] >= 0 else "red"
        console.print(f"  Income:        [green]${cf['income']:>10,.2f}[/]  ({cf['income_txns']} txns)")
        console.print(f"  Expenses:      [red]${cf['expenses']:>10,.2f}[/]  ({cf['expense_txns']} txns)")
        console.print(f"  Net:           [{net_color}]${cf['net']:>10,.2f}[/]")
        console.print(f"  Transfers in:  ${cf['transfers_in']:>10,.2f}")
        console.print(f"  Transfers out: ${cf['transfers_out']:>10,.2f}")
        if cf["internal_pairs"]:
            console.print(f"  Internal pairs detected: {cf['internal_pairs']}")

        if cf["by_income_category"]:
            console.print()
            income_table = Table(title="Income Sources")
            income_table.add_column("Category")
            income_table.add_column("Amount", justify="right")
            income_table.add_column("Txns", justify="right")
            for r in cf["by_income_category"]:
                income_table.add_row(r["category"], f"${r['amount']:,.2f}", str(r["count"]))
            console.print(income_table)

        if months > 1:
            trend = cashflow_trend(conn, months=months, transfer_categories=transfer_cats)
            trend_table = Table(title=f"Cashflow Trend (last {months} months)")
            trend_table.add_column("Month")
            trend_table.add_column("Income", justify="right")
            trend_table.add_column("Expenses", justify="right")
            trend_table.add_column("Net", justify="right")
            for row in trend:
                net = row["net"]
                net_str = f"[green]${net:,.2f}[/]" if net >= 0 else f"[red]${net:,.2f}[/]"
                trend_table.add_row(
                    f"{row['year']}-{row['month']:02d}",
                    f"${row['income']:,.2f}", f"${row['expenses']:,.2f}", net_str,
                )
            console.print(trend_table)
    finally:
        conn.close()


# -- review --------------------------------------------------------------------

@app.command()
def review(
    limit: int = typer.Option(50, "--limit", "-n", help="Max transactions to review"),
    min_confidence: float = typer.Option(0.60, "--confidence", "-c",
                                          help="Flag transactions below this confidence"),
):
    """
    Interactively review low-confidence or uncategorized transactions.

    For each transaction, enter a new category (or press Enter to skip, q to quit).
    Corrections are written immediately to the local DB.
    """
    from .overrides import get_review_candidates, PLAID_CATEGORIES
    from .db import set_override

    conn = _open_db()
    try:
        candidates = get_review_candidates(conn, limit=limit, min_confidence=min_confidence)
        if not candidates:
            console.print("[green]No transactions need review at this confidence threshold.[/]")
            return

        console.print(f"\nReviewing [bold]{len(candidates)}[/] transaction(s). Press Enter to skip, q to quit.\n")
        console.print("Categories: " + ", ".join(PLAID_CATEGORIES) + "\n")

        reviewed = 0
        for txn in candidates:
            console.print(
                f"[bold]{txn['date']}[/]  [cyan]{txn['merchant']:<35}[/]  "
                f"[yellow]${txn['amount']:>8.2f}[/]  "
                f"{txn['category_primary'] or 'NONE':<25}  "
                f"conf={txn['confidence']:.2f}  [{txn['category_source']}]"
            )
            try:
                raw = input("  New category (Enter=skip, q=quit): ").strip()
            except (EOFError, KeyboardInterrupt):
                break

            if raw.lower() == "q":
                break
            if not raw:
                continue

            cat = raw.upper()
            if cat not in PLAID_CATEGORIES:
                console.print(f"  [yellow]Unknown category '{cat}' -- saving anyway.[/]")

            note_raw = input("  Note (optional, Enter=skip): ").strip()
            set_override(conn, txn["transaction_id"], cat, note=note_raw or None)
            conn.commit()
            console.print(f"  [green]Saved: {txn['merchant']} -> {cat}[/]")
            reviewed += 1

        console.print(f"\n[bold]Done.[/] Reviewed {reviewed} transaction(s).")
    finally:
        conn.close()


# -- push (Google Sheets) ------------------------------------------------------

@app.command()
def push(
    month: Optional[str] = typer.Option(None, "--month", "-m",
                                         help="Month for Summary tab (YYYY-MM)"),
    trend_months: int = typer.Option(12, "--trends", help="Months of history in Trends tab"),
    txn_days: int = typer.Option(90, "--days", help="Days of transactions in Transactions tab"),
    include_forecast: bool = typer.Option(False, "--forecast",
                                           help="Include Forecast tab (requires prophet)"),
):
    """Push financial data to Google Sheets (Summary, Trends, Cashflow, Transactions tabs)."""
    from .sheets import FintrackSheetsClient
    s = get_settings()
    if not s.google_spreadsheet_id:
        console.print("[red]GOOGLE_SPREADSHEET_ID not set in .env -- cannot push.[/]")
        raise typer.Exit(1)

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
        client = FintrackSheetsClient(s.google_service_account_file, s.google_spreadsheet_id)

        with console.status("  Pulling Sheets overrides..."):
            n_overrides = client.pull_overrides(conn)
        if n_overrides:
            console.print(f"  [green]v[/] Pulled {n_overrides} override(s) from Sheets")

        with console.status("  Writing Summary..."):
            client.push_summary(conn, year, mon)
        console.print("  [green]v[/] Summary")

        with console.status("  Writing Trends..."):
            client.push_trends(conn, months=trend_months)
        console.print(f"  [green]v[/] Trends ({trend_months} months)")

        with console.status("  Writing Cashflow..."):
            client.push_cashflow(conn, year, mon,
                                 transfer_categories=s.get_cashflow_transfer_categories())
        console.print("  [green]v[/] Cashflow")

        with console.status("  Writing Transactions..."):
            client.push_transactions(conn, days=txn_days)
        console.print(f"  [green]v[/] Transactions (last {txn_days} days)")

        if include_forecast:
            with console.status("  Writing Forecast... (this may take a minute)"):
                try:
                    client.push_forecast(conn)
                    console.print("  [green]v[/] Forecast")
                except ImportError as e:
                    console.print(f"  [yellow]Forecast skipped: {e}[/]")

        console.print(
            "\n[bold green]Done![/] "
            "https://docs.google.com/spreadsheets/d/" + s.google_spreadsheet_id
        )
    finally:
        conn.close()


# -- check (alerts) ------------------------------------------------------------

@app.command()
def check(
    send: bool = typer.Option(True, "--send/--no-send",
                              help="Send via ntfy.sh (default: yes if NTFY_TOPIC set)"),
):
    """Run all alert checks and optionally push via ntfy.sh."""
    from .alerts import run_all_checks, send_batch, P_URGENT, P_WARNING, P_INFO

    s = get_settings()
    conn = _open_db()
    try:
        charges = _load_recurring(conn)
        alerts = run_all_checks(
            conn,
            charges,
            large_txn_threshold=s.large_transaction_threshold,
            large_txn_lookback_days=s.large_transaction_lookback_days,
            spending_spike_pct=s.spending_spike_pct,
            upcoming_days=s.recurring_upcoming_days,
            missing_grace_days=s.recurring_missing_grace_days,
        )

        if not alerts:
            console.print("[green]All clear -- no alerts.[/]")
            return

        priority_icon = {P_URGENT: "[bold red]!", P_WARNING: "[yellow]~", P_INFO: "[blue]i"}
        for a in alerts:
            icon = priority_icon.get(a.get("priority", P_WARNING), "~")
            console.print(f"  {icon}[/] {a['title']}")

        console.print(f"\n{len(alerts)} alert(s) found.")

        if send and s.ntfy_topic:
            with console.status(f"  Sending to ntfy.sh/{s.ntfy_topic}..."):
                n = send_batch(alerts, ntfy_topic=s.ntfy_topic,
                               ntfy_server=s.ntfy_server, conn=conn)
                conn.commit()
            console.print(f"  [green]v[/] Sent {n}/{len(alerts)} alert(s) via ntfy.sh")
        elif send and not s.ntfy_topic:
            console.print("  [dim]NTFY_TOPIC not set -- alerts logged only.[/]")
    finally:
        conn.close()


# -- forecast ------------------------------------------------------------------

@app.command()
def forecast(
    months_ahead: int = typer.Option(3, "--ahead", help="Months to forecast"),
    show_anomalies: bool = typer.Option(True, "--anomalies/--no-anomalies",
                                         help="Show anomalous historical months"),
):
    """Spending forecast using Prophet (requires: pip install prophet)."""
    from .forecasting import forecast_all_categories, detect_anomalous_months

    conn = _open_db()
    try:
        console.print(f"Forecasting next {months_ahead} month(s)...")
        try:
            forecasts = forecast_all_categories(conn, months_ahead=months_ahead)
        except ImportError as e:
            console.print(f"[red]{e}[/]")
            raise typer.Exit(1)

        if not forecasts:
            console.print("[yellow]Not enough history to generate forecasts yet.[/]")
            return

        fc_table = Table(title=f"Spending Forecast -- next {months_ahead} month(s)")
        fc_table.add_column("Category")
        fc_table.add_column("Month")
        fc_table.add_column("Forecast", justify="right")
        fc_table.add_column("Low", justify="right")
        fc_table.add_column("High", justify="right")

        for cat, rows in forecasts.items():
            future = [r for r in rows if r["is_forecast"]]
            for r in future:
                fc_table.add_row(
                    cat, r["ds"][:7],
                    f"${r['yhat']:,.2f}", f"${r['yhat_lower']:,.2f}", f"${r['yhat_upper']:,.2f}",
                )
        console.print(fc_table)

        if show_anomalies:
            s = get_settings()
            anomalies = detect_anomalous_months(conn, sigma_threshold=s.forecast_anomaly_sigma)
            if anomalies:
                an_table = Table(title="Anomalous Historical Months")
                an_table.add_column("Category")
                an_table.add_column("Month")
                an_table.add_column("Actual", justify="right")
                an_table.add_column("Expected", justify="right")
                an_table.add_column("Sigma", justify="right")
                an_table.add_column("Method")
                for a in anomalies:
                    an_table.add_row(
                        a["category"], a["month_ds"],
                        f"${a['actual']:,.2f}", f"${a['expected']:,.2f}",
                        str(a["sigma"]), a["method"],
                    )
                console.print(an_table)
    finally:
        conn.close()


# -- reauth --------------------------------------------------------------------

@app.command()
def reauth(
    item: str = typer.Option(..., "--item", "-i", help="item_id to re-authenticate"),
    port: Optional[int] = typer.Option(None, help="Override LINK_SERVER_PORT"),
):
    """Launch the Plaid Link update flow to fix a broken/expired item."""
    import subprocess, sys
    s = get_settings()
    p = port or s.link_server_port
    console.print(f"\n[bold yellow]Starting reauth server for item {item[:8]}...[/]")
    console.print(f"Open http://localhost:{p} in your browser to complete re-authentication.\n")
    subprocess.run(
        [sys.executable, "-m", "link_server.server"],
        env={**__import__("os").environ, "LINK_SERVER_PORT": str(p), "REAUTH_ITEM_ID": item},
    )


# -- keygen --------------------------------------------------------------------

@app.command()
def keygen():
    """Generate a Fernet key for FERNET_KEY in your .env (encrypts stored tokens)."""
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    console.print(f"\nAdd to your [bold].env[/]:\n\n  [green]FERNET_KEY={key}[/]\n")
    console.print("[yellow]Keep this key safe -- losing it means stored tokens become unreadable.[/]")


# -- items list ----------------------------------------------------------------

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
        table.add_column("Status", justify="center")
        for item in items:
            error = item.get("error_state")
            status_cell = (f"[bold red]x {error}[/]" if error
                           else "[green]OK[/]" if item["cursor"] else "[yellow]pending[/]")
            table.add_row(item["item_id"], item["institution_name"],
                          item["last_synced"] or "never", status_cell)
        console.print(table)
    finally:
        conn.close()
