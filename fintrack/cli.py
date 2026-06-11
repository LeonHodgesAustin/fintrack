"""
fintrack CLI -- Typer-based interface.

Commands:
  fintrack link              Start link server to connect an institution
  fintrack sync              Sync transactions for all linked items
  fintrack report spending   Monthly spending summary
  fintrack report networth   Net worth time-series from balance snapshots
  fintrack flag <txn_id>     Flag a transaction as out-of-band
  fintrack flag list         Show all flagged transactions
  fintrack cashflow          Net cashflow (income vs expenses)
  fintrack review            Interactive review of low-confidence transactions
  fintrack push              Push data to Google Sheets
  fintrack check             Run all alert checks and send via ntfy.sh
  fintrack forecast          Spending forecasts (requires prophet)
  fintrack networth          Net worth snapshot across all asset types
  fintrack reauth --item ID  Trigger re-authentication for an item
  fintrack keygen            Generate a Fernet encryption key
  fintrack items list        Show linked institutions and sync status

  fintrack assets loan add        Add a mortgage or auto loan
  fintrack assets loan list       List loans and current balances
  fintrack assets loan schedule   Show amortization schedule
  fintrack assets vehicle add     Add a vehicle for depreciation tracking
  fintrack assets vehicle list    List vehicles and estimated values
  fintrack assets equity add-rsu  Add an RSU grant
  fintrack assets equity add-espp Add an ESPP plan
  fintrack assets equity list     Show grants, vested shares, and values
  fintrack assets equity record   Record a vest, sale, or ESPP purchase
  fintrack assets equity scan     Scan transactions for potential stock sales
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

assets_app    = typer.Typer(help="Manage manual assets: loans, vehicles, properties, equity, accounts.")
loans_app     = typer.Typer(help="Mortgage and auto loan tracking.")
vehicles_app  = typer.Typer(help="Vehicle depreciation tracking.")
property_app  = typer.Typer(help="Real estate / property tracking.")
equity_app    = typer.Typer(help="RSU and ESPP equity tracking.")
account_app   = typer.Typer(help="Manual accounts: 401k, IRA, HSA, pension, etc.")
app.add_typer(assets_app, name="assets")
assets_app.add_typer(loans_app,    name="loan")
assets_app.add_typer(vehicles_app, name="vehicle")
assets_app.add_typer(property_app, name="property")
assets_app.add_typer(equity_app,   name="equity")
assets_app.add_typer(account_app,  name="account")

report_app = typer.Typer(help="Financial reports: spending summaries, net worth history, etc.")
app.add_typer(report_app, name="report")

flag_app = typer.Typer(help="Flag transactions as out-of-band (one-time, reimbursable, gift, etc.).")
app.add_typer(flag_app, name="flag")

adjust_app = typer.Typer(help="Saved budget normalizations (annual→monthly, one-time items, etc.)")
app.add_typer(adjust_app, name="adjust")

target_app = typer.Typer(help="Per-category monthly spending targets.")
app.add_typer(target_app, name="target")

tax_app      = typer.Typer(help="Tax-prep helpers: tag transactions, track documents, store reference info.")
tax_tag_app  = typer.Typer(help="Tag transactions with tax categories for year-end reporting.")
tax_docs_app = typer.Typer(help="Track expected and received tax documents (W-2, 1099s, etc.).")
tax_info_app = typer.Typer(help="Store static tax reference info (EIN, prior-year AGI, etc.).")
app.add_typer(tax_app,  name="tax")
tax_app.add_typer(tax_tag_app,  name="tag")
tax_app.add_typer(tax_docs_app, name="docs")
tax_app.add_typer(tax_info_app, name="info")

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
    from .assets.db import migrate as migrate_assets
    migrate_assets(s.db_path)
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


# -- report -------------------------------------------------------------------

@report_app.command("spending")
def report_spending(
    month: Optional[str] = typer.Option(None, "--month", "-m", help="Month (YYYY-MM)"),
    top: int = typer.Option(10, "--top", "-t", help="Number of top merchants to show"),
    exclude_flagged: bool = typer.Option(
        False, "--exclude-flagged",
        help="Omit flagged transactions from totals and show both raw and normalized spend.",
    ),
):
    """Print a monthly spending summary to stdout."""
    from .reports import monthly_summary, top_merchants, mom_trends, flagged_in_period
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
        flagged_info = flagged_in_period(conn, year, mon) if exclude_flagged else None
        summary = monthly_summary(conn, year, mon, exclude_flagged=exclude_flagged)
        total = sum(r["total_amount"] for r in summary)

        footer_label = f"${total:,.2f}"
        if flagged_info and flagged_info["count"]:
            footer_label += f"  [dim](normalized)[/]"

        cat_table = Table(title=f"Spending by Category -- {year}-{mon:02d}", show_footer=True)
        cat_table.add_column("Category")
        cat_table.add_column("Amount", justify="right", footer=footer_label)
        cat_table.add_column("Txns", justify="right")
        cat_table.add_column("%", justify="right")
        for row in summary:
            pct = (row["total_amount"] / total * 100) if total else 0
            cat_table.add_row(
                row["category"], f"${row['total_amount']:,.2f}",
                str(row["transaction_count"]), f"{pct:.1f}%",
            )
        console.print(cat_table)

        if flagged_info and flagged_info["count"]:
            raw_total = total + flagged_info["total_amount"]
            console.print(
                f"  [dim]Raw total (incl. {flagged_info['count']} flagged txn(s)): "
                f"${raw_total:,.2f}[/]"
            )
            console.print(
                f"  [green]Normalized spend (excl. {flagged_info['count']} flagged): "
                f"${total:,.2f}[/]  "
                f"[dim](${flagged_info['total_amount']:,.2f} excluded)[/]"
            )

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


@report_app.command("networth")
def report_networth(
    limit: int = typer.Option(30, "--limit", "-n", help="Number of snapshots to show (newest first)"),
):
    """
    Show net worth over time from balance snapshots captured during sync.

    Each row is one sync run (hour-bucketed). The Change column is colored
    green when net worth increased and red when it decreased.
    """
    conn = _open_db()
    try:
        rows = conn.execute(
            """
            SELECT snapshot_hour, total_assets, total_liabilities, net_worth
            FROM net_worth_snapshots
            ORDER BY snapshot_hour DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        if not rows:
            console.print(
                "[yellow]No balance snapshots yet. Run [bold]fintrack sync[/] "
                "to capture balances.[/]"
            )
            return

        rows = list(reversed(rows))  # oldest → newest for delta calculation

        t = Table(title=f"Net Worth History (last {len(rows)} sync(s))")
        t.add_column("Date/Time")
        t.add_column("Assets", justify="right")
        t.add_column("Liabilities", justify="right")
        t.add_column("Net Worth", justify="right")
        t.add_column("Change", justify="right")

        for i, row in enumerate(rows):
            delta_str = ""
            if i > 0:
                delta = row["net_worth"] - rows[i - 1]["net_worth"]
                sign = "+" if delta >= 0 else ""
                color = "green" if delta >= 0 else "red"
                delta_str = f"[{color}]{sign}${delta:,.2f}[/]"

            nw = row["net_worth"]
            nw_color = "green" if nw >= 0 else "red"
            t.add_row(
                row["snapshot_hour"][:16].replace("T", " "),
                f"${row['total_assets']:,.2f}",
                f"${row['total_liabilities']:,.2f}",
                f"[{nw_color}]${nw:,.2f}[/]",
                delta_str,
            )

        console.print(t)

        latest = rows[-1]
        nw_color = "green" if latest["net_worth"] >= 0 else "red"
        console.print(
            f"\n  [bold]Current Net Worth: [{nw_color}]${latest['net_worth']:,.2f}[/][/]"
        )
    finally:
        conn.close()


@report_app.command("tax-summary")
def report_tax_summary(
    year: int = typer.Option(
        None, "--year", "-y",
        help="Tax year to summarize (default: current calendar year)",
    ),
    detail: bool = typer.Option(False, "--detail", "-d", help="Show individual transactions per category"),
):
    """
    Yearly spending totals grouped by tax category.

    Shows totals for every category you have tagged with 'fintrack tax tag'.
    Use --detail to list individual transactions under each category.

      fintrack report tax-summary --year 2025
      fintrack report tax-summary --year 2025 --detail
    """
    from .reports import tax_summary

    if year is None:
        year = date.today().year

    conn = _open_db()
    try:
        rows = tax_summary(conn, year)
        if not rows:
            console.print(
                f"[yellow]No tax-tagged transactions for {year}. "
                f"Use [bold]fintrack tax tag <txn_id>[/] to tag transactions.[/]"
            )
            return

        total = sum(r["total_amount"] for r in rows)
        t = Table(title=f"Tax Summary — {year}  (${total:,.2f} total tagged)", show_footer=True)
        t.add_column("Category")
        t.add_column("Amount", justify="right", footer=f"${total:,.2f}")
        t.add_column("Txns", justify="right")
        t.add_column("Notes / Examples", style="dim")

        for r in rows:
            example_notes = ", ".join(
                filter(None, [tx.get("note") for tx in r["transactions"][:2]])
            )
            t.add_row(
                r["tax_category"],
                f"${r['total_amount']:,.2f}",
                str(r["transaction_count"]),
                example_notes[:50],
            )
        console.print(t)

        if detail:
            for r in rows:
                dt = Table(title=f"  {r['tax_category']} — ${r['total_amount']:,.2f}", box=None)
                dt.add_column("Tag #", justify="right", style="dim")
                dt.add_column("Date")
                dt.add_column("Merchant")
                dt.add_column("Amount", justify="right")
                dt.add_column("Note", style="dim")
                for tx in r["transactions"]:
                    dt.add_row(
                        str(tx["tag_id"]),
                        tx["date"],
                        (tx["merchant"] or "")[:40],
                        f"${tx['amount']:,.2f}",
                        (tx["note"] or "")[:40],
                    )
                console.print(dt)
    finally:
        conn.close()


# -- flag ---------------------------------------------------------------------

FLAG_TYPE_CHOICES = ["one-time", "reimbursable", "gift", "transfer", "other"]


@flag_app.callback(invoke_without_command=True)
def flag_add(
    ctx: typer.Context,
    transaction_id: Optional[str] = typer.Argument(
        None, help="Transaction ID to flag (from 'fintrack report spending' or the DB)."
    ),
    type_: Optional[str] = typer.Option(
        None, "--type", "-t",
        help=f"Flag type: {', '.join(FLAG_TYPE_CHOICES)}",
    ),
    note: Optional[str] = typer.Option(
        None, "--note", "-n", help="Optional explanation, e.g. 'uncle 70th birthday flight'."
    ),
):
    """
    Flag a transaction as out-of-band so it can be excluded from spending reports.

      fintrack flag txn-abc123 --type one-time --note "wedding gift"
      fintrack flag txn-abc123 --type reimbursable --note "expensed to work"

    Use 'fintrack flag list' to review all flagged transactions.
    Use 'fintrack report spending --exclude-flagged' to see spend without them.
    """
    if ctx.invoked_subcommand is not None:
        return

    if not transaction_id:
        console.print(ctx.get_help())
        raise typer.Exit(0)

    if not type_:
        console.print(f"[red]--type is required. Choices: {', '.join(FLAG_TYPE_CHOICES)}[/]")
        raise typer.Exit(1)

    if type_ not in FLAG_TYPE_CHOICES:
        console.print(
            f"[red]Unknown flag type '{type_}'. "
            f"Valid types: {', '.join(FLAG_TYPE_CHOICES)}[/]"
        )
        raise typer.Exit(1)

    from .db import add_flag, FLAG_TYPES

    conn = _open_db()
    try:
        txn = conn.execute(
            """
            SELECT t.transaction_id, t.date, t.amount,
                   COALESCE(t.merchant_name, t.raw_name, 'Unknown') AS merchant
            FROM transactions t
            WHERE t.transaction_id = ?
            """,
            (transaction_id,),
        ).fetchone()

        if not txn:
            console.print(f"[red]Transaction '{transaction_id}' not found.[/]")
            raise typer.Exit(1)

        flag_id = add_flag(conn, transaction_id, type_, note)
        conn.commit()

        console.print(
            f"[green]Flagged[/] [bold]{txn['merchant']}[/] "
            f"(${txn['amount']:,.2f} on {txn['date']}) "
            f"as [bold]{type_}[/] [dim][flag #{flag_id}][/]"
        )
        if note:
            console.print(f"  Note: {note}")
    finally:
        conn.close()


@flag_app.command("list")
def flag_list():
    """Show all flagged transactions."""
    from .db import get_all_flags

    conn = _open_db()
    try:
        flags = get_all_flags(conn)
        if not flags:
            console.print(
                "[dim]No flagged transactions. "
                "Use [bold]fintrack flag <txn_id> --type <type>[/] to add one.[/]"
            )
            return

        t = Table(title=f"Flagged Transactions ({len(flags)} total)")
        t.add_column("Flag #", justify="right", style="dim")
        t.add_column("Date")
        t.add_column("Merchant")
        t.add_column("Amount", justify="right")
        t.add_column("Type")
        t.add_column("Note", style="dim")
        t.add_column("Transaction ID", style="dim")

        for f in flags:
            t.add_row(
                str(f["flag_id"]),
                f["date"],
                (f["merchant"] or "")[:35],
                f"${f['amount']:,.2f}",
                f["flag_type"],
                (f["note"] or "")[:40],
                f["transaction_id"][:20] + "…" if len(f["transaction_id"]) > 20 else f["transaction_id"],
            )

        console.print(t)
        total_flagged = sum(f["amount"] for f in flags)
        console.print(f"\n  Total flagged spend: [bold]${total_flagged:,.2f}[/]")
    finally:
        conn.close()


# -- budget --------------------------------------------------------------------

@app.command()
def budget(
    months: int = typer.Option(3, "--months", "-m",
                               help="Months of history to average (default 3)"),
    set_income: Optional[float] = typer.Option(
        None, "--income", "-i",
        help="Override detected income with a known monthly take-home (e.g. 6500). "
             "Use this to model against payroll only, ignoring investment returns "
             "and other non-reliable credits.",
    ),
    what_if: Optional[str] = typer.Option(
        None, "--what-if", "-w",
        help="Scenario to model as 'Label:amount' — e.g. 'Child support increase:400'. "
             "Repeat to stack multiple changes.",
        show_default=False,
    ),
    what_if_extra: list[str] = typer.Option(
        [], "--also", "-a",
        help="Additional scenario changes (same format as --what-if).",
    ),
):
    """
    Monthly budget breakdown with optional what-if scenario modeling.

    Shows averaged income, fixed loan payments, recurring charges, variable
    spending, and transfers — then calculates your monthly surplus.

    To model a change before committing to it:

      fintrack budget --what-if "Child support increase:400"
      fintrack budget --what-if "Child support increase:400" --also "Cut Dining:-150"
    """
    from .budget import build_budget, ScenarioChange

    s = get_settings()
    conn = _open_db()
    try:
        snap = build_budget(conn, months=months,
                            transfer_categories=s.get_cashflow_transfer_categories(),
                            savings_merchants=s.get_savings_transfer_merchants())

        if set_income is not None:
            snap.avg_income = set_income

        # ── Header ─────────────────────────────────────────────────────────
        console.print(
            f"\n[bold]Monthly Budget — {months}-month average "
            f"({snap.period_label})[/]\n"
        )

        # ── Income ─────────────────────────────────────────────────────────
        income_table = Table(title="Income", box=None, show_header=False, padding=(0, 2))
        income_table.add_column(justify="left",  style="dim")
        income_table.add_column(justify="right")
        if set_income is not None:
            income_table.add_row("[dim]Transaction-detected (not used)[/]",
                                 f"[dim]${sum(r['avg_amount'] for r in snap.income_by_source):,.2f}/mo[/]")
            income_table.add_row("Payroll (manual override)", f"${set_income:,.2f}/mo")
        else:
            for src in snap.income_by_source:
                income_table.add_row(src["category"], f"${src['avg_amount']:,.2f}/mo")
        income_table.add_row("[bold]Total income[/]", f"[bold green]${snap.avg_income:,.2f}/mo[/]")
        console.print(income_table)

        # ── Fixed obligations ───────────────────────────────────────────────
        if snap.loan_payments or snap.recurring:
            console.print()
            fixed_table = Table(title="Fixed Obligations", box=None, show_header=False, padding=(0, 2))
            fixed_table.add_column(justify="left",  style="dim")
            fixed_table.add_column(justify="right")
            for l in snap.loan_payments:
                fixed_table.add_row(
                    f"{l['name']} ({l['type']}, {l['rate_pct']:.2f}%)",
                    f"[red]-${l['monthly_payment']:,.2f}/mo[/]",
                )
            for r in snap.recurring:
                src_tag = "" if r["source"] == "manual" else f" [dim][auto][/]"
                fixed_table.add_row(
                    f"{r['label']}{src_tag}",
                    f"[red]-${r['monthly_amount']:,.2f}/mo[/]",
                )
            fixed_table.add_row(
                "[bold]Total fixed[/]",
                f"[bold red]-${snap.total_obligations:,.2f}/mo[/]",
            )
            console.print(fixed_table)

        # ── Variable spending ───────────────────────────────────────────────
        if snap.variable:
            console.print()
            has_targets = bool(snap.targets)
            var_table = Table(title="Variable Spending (avg)", box=None,
                              show_header=False, padding=(0, 2))
            var_table.add_column(justify="left",  style="dim")
            var_table.add_column(justify="right")
            if has_targets:
                var_table.add_column(justify="left", style="dim")  # target column
            for v in snap.variable:
                cat = v["category"]
                amt_str = f"[yellow]-${v['avg_amount']:,.2f}/mo[/]"
                if has_targets:
                    if cat in snap.targets:
                        tgt = snap.targets[cat]["target_amount"]
                        over = v["avg_amount"] - tgt
                        if over > 0:
                            tgt_str = f"[red]target ${tgt:,.0f}  OVER ${over:,.0f}[/]"
                        else:
                            tgt_str = f"[green]target ${tgt:,.0f}  under ${abs(over):,.0f}[/]"
                    else:
                        tgt_str = ""
                    var_table.add_row(cat, amt_str, tgt_str)
                else:
                    var_table.add_row(cat, amt_str)
            if has_targets:
                var_table.add_row("[bold]Total variable[/]",
                                  f"[bold yellow]-${snap.total_variable:,.2f}/mo[/]", "")
            else:
                var_table.add_row("[bold]Total variable[/]",
                                  f"[bold yellow]-${snap.total_variable:,.2f}/mo[/]")
            console.print(var_table)

        # ── Transfers out ───────────────────────────────────────────────────
        if snap.transfer_detail:
            console.print()
            tr_table = Table(title="Transfers Out (avg)", box=None, show_header=False, padding=(0, 2))
            tr_table.add_column(justify="left",  style="dim")
            tr_table.add_column(justify="right")
            for t in snap.transfer_detail:
                tr_table.add_row(t["merchant"], f"[blue]-${t['avg_amount']:,.2f}/mo[/]")
            tr_table.add_row("[bold]Total transfers[/]",
                             f"[bold blue]-${snap.transfers_out:,.2f}/mo[/]")
            console.print(tr_table)

        if snap.savings_detail:
            console.print()
            sv_table = Table(
                title="Savings Transfers (avg) [dim]-- still your money, not counted in deficit[/]",
                box=None, show_header=False, padding=(0, 2),
            )
            sv_table.add_column(justify="left",  style="dim")
            sv_table.add_column(justify="right")
            for t in snap.savings_detail:
                sv_table.add_row(t["merchant"], f"[cyan]${t['avg_amount']:,.2f}/mo[/]")
            sv_table.add_row("[bold]Total savings[/]",
                             f"[bold cyan]${snap.savings_out:,.2f}/mo[/]")
            console.print(sv_table)

        # ── Surplus ─────────────────────────────────────────────────────────
        console.print()
        if snap.saved_adjustments:
            # Show raw → adjustments → adjusted breakdown
            raw_color = "green" if snap.raw_surplus >= 0 else "red"
            console.print(
                f"  Monthly surplus (raw):        [{raw_color}]${snap.raw_surplus:,.2f}/mo[/]"
            )
            console.print()

            adj_table = Table(
                title=f"Saved Normalizations ({len(snap.saved_adjustments)} active)",
                box=None, show_header=False, padding=(0, 2),
            )
            adj_table.add_column("id",    justify="right", style="dim")
            adj_table.add_column("label", justify="left",  style="dim")
            adj_table.add_column("amt",   justify="right")
            adj_table.add_column("cat",   justify="left",  style="dim")
            for a in snap.saved_adjustments:
                amt = a["monthly_amount"]
                color = "green" if amt < 0 else "red"
                sign  = "+" if amt > 0 else ""
                cat_tag = f"({a['category']})" if a.get("category") else ""
                adj_table.add_row(
                    f"[{a['id']}]",
                    a["label"],
                    f"[{color}]{sign}${amt:,.2f}/mo[/]",
                    cat_tag,
                )
            net = snap.total_saved_adjustments
            net_sign  = "+" if net > 0 else ""
            net_color = "green" if net < 0 else "red"
            adj_table.add_row(
                "", "[bold]Net adjustments[/]",
                f"[bold {net_color}]{net_sign}${net:,.2f}/mo[/]", "",
            )
            console.print(adj_table)
            console.print()

        surplus_color = "green" if snap.surplus >= 0 else "red"
        surplus_label = "Monthly surplus (adjusted):" if snap.saved_adjustments else "Monthly surplus:          "
        console.print(
            f"  [bold]{surplus_label}  [{surplus_color}]${snap.surplus:,.2f}/mo[/][/]"
        )

        # ── What-if scenario ────────────────────────────────────────────────
        scenarios: list[ScenarioChange] = []
        raw_scenarios = ([what_if] if what_if else []) + list(what_if_extra)

        for raw in raw_scenarios:
            try:
                label, amt_str = raw.rsplit(":", 1)
                scenarios.append(ScenarioChange(label.strip(), float(amt_str.strip())))
            except ValueError:
                console.print(f"[yellow]Could not parse scenario '{raw}' — use 'Label:amount'[/]")

        if scenarios:
            console.print()
            console.rule("[bold]What-If Scenario[/]")
            for sc in scenarios:
                sign = "+" if sc.monthly_amount >= 0 else ""
                color = "red" if sc.monthly_amount > 0 else "green"
                console.print(
                    f"  {sc.label}:  [{color}]{sign}${sc.monthly_amount:,.2f}/mo[/]"
                )
            new_surplus = snap.model_scenario(scenarios)
            new_color = "green" if new_surplus >= 0 else "red"
            delta = new_surplus - snap.surplus
            delta_sign = "+" if delta >= 0 else ""
            console.print()
            console.print(
                f"  Current surplus:   ${snap.surplus:,.2f}/mo\n"
                f"  Scenario delta:    {delta_sign}${delta:,.2f}/mo\n"
                f"  [bold]Adjusted surplus:  [{new_color}]${new_surplus:,.2f}/mo[/][/]"
            )
            if new_surplus >= 0:
                console.print(
                    f"\n  [green][OK] Affordable.[/] After this change you would still have "
                    f"[bold]${new_surplus:,.2f}/mo[/] left over."
                )
            else:
                shortfall = abs(new_surplus)
                console.print(
                    f"\n  [red][NO] Not affordable as-is.[/] This change creates a "
                    f"[bold]${shortfall:,.2f}/mo shortfall[/]. "
                    f"You would need to cut ${shortfall:,.2f}/mo from variable spending or transfers."
                )

    finally:
        conn.close()


# -- drill ---------------------------------------------------------------------

@app.command()
def drill(
    category: str = typer.Argument(..., help="Category to inspect (e.g. CHILDCARE, FOOD_AND_DRINK)"),
    months:   int = typer.Option(3,    "--months", "-m", help="Months to look back"),
    limit:    int = typer.Option(50,   "--limit",  "-n", help="Max transactions to show"),
):
    """
    Show every transaction behind a budget category so you can see what's
    actually driving the number.

      fintrack drill CHILDCARE
      fintrack drill FOOD_AND_DRINK --months 1
    """
    from datetime import date as _date

    today = _date.today()
    m = today.month - months
    y = today.year
    while m <= 0:
        m += 12
        y -= 1
    start = _date(y, m, 1).isoformat()
    end   = _date(today.year, today.month, 1).isoformat()

    conn = _open_db()
    try:
        rows = conn.execute(
            """
            SELECT
                t.date,
                COALESCE(t.merchant_name, t.raw_name, 'Unknown') AS merchant,
                t.amount,
                t.category_detailed,
                t.category_source,
                i.institution_name,
                a.name AS account_name
            FROM transactions t
            JOIN accounts a ON a.account_id = t.account_id
            JOIN items    i ON i.item_id    = a.item_id
            WHERE t.category_primary = ?
              AND t.date >= ? AND t.date < ?
              AND t.amount > 0
              AND t.pending = 0
            ORDER BY t.amount DESC
            LIMIT ?
            """,
            (category.upper(), start, end, limit),
        ).fetchall()

        if not rows:
            console.print(f"[yellow]No transactions found in {category} for the last {months} month(s).[/]")
            return

        total = sum(r["amount"] for r in rows)
        monthly_avg = total / months

        t = Table(title=f"{category} — last {months} month(s)  |  total ${total:,.2f}  |  avg ${monthly_avg:,.2f}/mo")
        t.add_column("Date")
        t.add_column("Merchant")
        t.add_column("Amount", justify="right")
        t.add_column("Subcategory", style="dim")
        t.add_column("Account", style="dim")

        for r in rows:
            t.add_row(
                r["date"],
                (r["merchant"] or "")[:40],
                f"${r['amount']:,.2f}",
                (r["category_detailed"] or "").replace(f"{category}_", "").replace("_", " ").lower(),
                f"{r['institution_name'][:12]} / {r['account_name'][:12]}",
            )
        console.print(t)
        console.print(f"\n  [dim]Showing top {min(limit, len(rows))} of {len(rows)} transactions. "
                      f"Use --months or --limit to adjust.[/]")
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


# ══════════════════════════════════════════════════════════════════════════════
# Net worth
# ══════════════════════════════════════════════════════════════════════════════

@app.command()
def networth(
    offline: bool = typer.Option(False, "--offline", help="Use cached prices, skip yfinance fetch"),
):
    """Net worth snapshot: liquid + equity + vehicles − loans."""
    from .assets.net_worth import snapshot

    conn = _open_db()
    try:
        with console.status("Building net worth snapshot..."):
            snap = snapshot(conn, fetch_prices=not offline)

        console.print(f"\n[bold]Net Worth Snapshot — {snap.as_of}[/]\n")

        nw_color = "green" if snap.net_worth >= 0 else "red"
        console.print(f"  [bold]Net Worth:  [{nw_color}]${snap.net_worth:>14,.2f}[/][/]")
        console.print(f"  Total Assets:       ${snap.total_assets:>14,.2f}")
        console.print(f"  Total Debt:        -${snap.total_debt:>14,.2f}")
        if snap.total_unvested_equity:
            console.print(f"  Unvested RSUs:      ${snap.total_unvested_equity:>14,.2f}  [dim](not counted)[/]")
        console.print()

        if snap.property_values:
            t = Table(title="Real Estate")
            t.add_column("Name"); t.add_column("Address")
            t.add_column("Purchase Price", justify="right")
            t.add_column("Est. Value", justify="right")
            t.add_column("Range", justify="right")
            t.add_column("Appreciation", justify="right")
            t.add_column("Home Equity", justify="right")
            t.add_column("As Of")
            for p in snap.property_values:
                if p["current_value"] is not None:
                    val_str = f"${p['current_value']:,.0f}"
                    app_str = (f"[green]+${p['appreciation']:,.0f} ({p['appreciation_pct']:.1f}%)[/]"
                               if p["appreciation"] and p["appreciation"] >= 0
                               else f"[red]-${abs(p['appreciation']):,.0f}[/]"
                               if p["appreciation"] else "")
                    eq_str  = f"${p['home_equity']:,.0f}" if p["home_equity"] is not None else "[dim]n/a[/]"
                    rng_str = (f"${p['value_range_low']:,.0f}–${p['value_range_high']:,.0f}"
                               if p["value_range_low"] and p["value_range_high"] else "[dim]—[/]")
                    upd_str = (p["value_updated_at"][:10] if p["value_updated_at"] else "[dim]never[/]")
                else:
                    val_str = "[yellow]not set[/]"
                    app_str = eq_str = rng_str = upd_str = "[dim]—[/]"
                t.add_row(p["name"], p["address"][:35], f"${p['purchase_price']:,.0f}",
                          val_str, rng_str, app_str, eq_str, upd_str)
            console.print(t)

        if snap.loan_balances:
            t = Table(title="Loans (Liabilities)")
            t.add_column("Name"); t.add_column("Type")
            t.add_column("Balance", justify="right")
            t.add_column("Rate", justify="right")
            t.add_column("Monthly Pmt", justify="right")
            for l in snap.loan_balances:
                t.add_row(l["name"], l["type"], f"${l['balance']:,.2f}",
                          f"{l['rate_pct']:.2f}%", f"${l['monthly_payment']:,.2f}")
            console.print(t)

        if snap.vehicle_values:
            t = Table(title="Vehicles")
            t.add_column("Name"); t.add_column("Purchase Price", justify="right")
            t.add_column("Est. Value", justify="right")
            t.add_column("Depreciation", justify="right")
            for v in snap.vehicle_values:
                t.add_row(v["name"], f"${v['purchase_price']:,.2f}",
                          f"${v['estimated_value']:,.2f}",
                          f"${v['depreciation']:,.2f} ({v['depreciation_pct']:.0f}%)")
            console.print(t)

        if snap.vested_equity:
            t = Table(title="Equity (Vested RSUs)")
            t.add_column("Ticker"); t.add_column("Grant Date")
            t.add_column("Vested Shares", justify="right")
            t.add_column("Price", justify="right")
            t.add_column("Value", justify="right")
            t.add_column("Unvested", justify="right")
            for e in snap.vested_equity:
                price_str = f"${e['price']:,.2f}" if e["price"] else "[dim]n/a[/]"
                t.add_row(e["ticker"], e["grant_date"],
                          f"{e['vested_shares']:.2f}", price_str,
                          f"${e['value']:,.2f}", f"{e['unvested']:.2f} shrs")
            console.print(t)

        if snap.espp_accrual:
            t = Table(title="ESPP (Current Period Accrual)")
            t.add_column("Ticker"); t.add_column("Period Start")
            t.add_column("Contributions", justify="right")
            t.add_column("Discount", justify="right")
            t.add_column("Current Price", justify="right")
            for e in snap.espp_accrual:
                price_str = f"${e['current_price']:,.2f}" if e["current_price"] else "[dim]n/a[/]"
                t.add_row(e["ticker"], e["period_start"],
                          f"${e['accrued_contributions']:,.2f}",
                          f"{e['discount_rate_pct']:.0f}%", price_str)
            console.print(t)

        if snap.manual_accounts:
            t = Table(title="Retirement & Other Accounts")
            t.add_column("Name"); t.add_column("Type")
            t.add_column("Institution"); t.add_column("Balance", justify="right")
            t.add_column("Last Updated")
            for a in snap.manual_accounts:
                bal_str = f"${a['balance']:,.2f}" if a["balance"] is not None else "[yellow]not set[/]"
                upd_str = a["updated_at"][:10] if a["updated_at"] else "[dim]never[/]"
                t.add_row(a["name"], a["type"], a["institution"], bal_str, upd_str)
            console.print(t)

    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# assets loan
# ══════════════════════════════════════════════════════════════════════════════

@loans_app.command("add")
def loan_add(
    name:      str   = typer.Option(..., "--name",       "-n", help="e.g. 'Primary Mortgage'"),
    type_:     str   = typer.Option("mortgage", "--type", "-t", help="mortgage|auto|personal|student"),
    principal: float = typer.Option(..., "--principal",  "-p",
                                    help="Balance as of --start date. "
                                         "Use the ORIGINAL amount + original first-payment date for a new loan, "
                                         "OR the CURRENT balance from your servicer + next payment date for an existing one."),
    rate:      float = typer.Option(..., "--rate",       "-r", help="Annual interest rate (e.g. 6.5 for 6.5%)"),
    term:      int   = typer.Option(..., "--term",
                                    help="Months REMAINING from --start date "
                                         "(e.g. 360 for a new 30yr; count from payoff date for an existing loan)"),
    start:     str   = typer.Option(..., "--start",      "-s",
                                    help="Date of first scheduled payment from --start balance (YYYY-MM-DD)"),
):
    """
    Add a loan (mortgage, auto, etc.).

    Two ways to enter an existing loan:

    \b
    From original docs (preferred):
      --principal 380000 --term 360 --start 2023-04-01

    From current servicer balance (when original docs aren't handy):
      --principal 287400 --term 284 --start 2026-07-01
      (use current balance, months remaining, next payment date)
    """
    from .assets.db import add_loan
    conn = _open_db()
    try:
        loan_id = add_loan(conn, name, type_, principal, rate / 100, term, start)
        conn.commit()
        console.print(f"[green]Added loan #{loan_id}: {name}[/]")
        console.print(f"  ${principal:,.2f} at {rate:.3f}% over {term} months starting {start}")
    finally:
        conn.close()


@loans_app.command("list")
def loan_list():
    """List all loans with current balances."""
    from .assets.db import get_loans
    from .assets.loans import from_db_row, current_balance, calculated_balance, monthly_payment, payoff_date

    conn = _open_db()
    try:
        rows = get_loans(conn)
        if not rows:
            console.print("[yellow]No loans. Add one with [bold]fintrack assets loan add[/].[/]")
            return
        t = Table(title="Loans")
        t.add_column("ID"); t.add_column("Name"); t.add_column("Type")
        t.add_column("Original", justify="right")
        t.add_column("Balance", justify="right")
        t.add_column("Rate", justify="right")
        t.add_column("Pmt/mo", justify="right")
        t.add_column("Payoff")
        for row in rows:
            loan = from_db_row(row)
            balance = current_balance(loan)
            if loan.actual_balance is not None:
                # Show actual balance with a marker; show calculated in dim for reference
                calc = calculated_balance(loan)
                updated = loan.balance_updated_at[:10] if loan.balance_updated_at else "?"
                bal_str = (f"[bold]${balance:,.2f}[/] [dim](sched: ${calc:,.2f}, "
                           f"set {updated})[/]")
            else:
                bal_str = f"${balance:,.2f}"
            t.add_row(
                str(loan.id), loan.name, loan.loan_type,
                f"${loan.principal:,.2f}", bal_str,
                f"{loan.annual_rate*100:.3f}%",
                f"${monthly_payment(loan):,.2f}",
                payoff_date(loan).isoformat(),
            )
        console.print(t)
    finally:
        conn.close()


@loans_app.command("update")
def loan_update(
    loan_id:   int            = typer.Argument(..., help="Loan ID from `fintrack assets loan list`"),
    principal: Optional[float] = typer.Option(None, "--principal", "-p",
                                              help="New balance as of --start date"),
    rate:      Optional[float] = typer.Option(None, "--rate", "-r",
                                              help="Annual interest rate (e.g. 6.5 for 6.5%)"),
    term:      Optional[int]   = typer.Option(None, "--term",
                                              help="Remaining months from --start date"),
    start:     Optional[str]   = typer.Option(None, "--start", "-s",
                                              help="Date balance is measured from (YYYY-MM-DD)"),
    payment:   Optional[float] = typer.Option(None, "--payment",
                                              help="Pin the exact monthly payment (overrides calculated value). "
                                                   "Useful when lender's figure differs slightly from the formula."),
):
    """
    Update one or more fields on an existing loan without deleting and re-adding it.

    Example — correct a wrong balance using servicer's current figure:
      fintrack assets loan update 2 --principal 32307.01 --term 59 --start 2026-07-01

    Example — pin the exact payment amount from your lender:
      fintrack assets loan update 2 --payment 649.05
    """
    from .assets.db import update_loan
    conn = _open_db()
    try:
        found = update_loan(
            conn, loan_id,
            principal=principal,
            annual_rate=rate / 100 if rate is not None else None,
            term_months=term,
            start_date=start,
            monthly_payment=payment,
        )
        if not found:
            console.print(f"[red]Loan #{loan_id} not found.[/]")
            raise typer.Exit(1)
        conn.commit()
        console.print(f"[green]Loan #{loan_id} updated.[/]")
    finally:
        conn.close()


@loans_app.command("delete")
def loan_delete(
    loan_id: int = typer.Argument(..., help="Loan ID from `fintrack assets loan list`"),
    confirm: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
):
    """Delete a loan record."""
    from .assets.db import delete_loan
    conn = _open_db()
    try:
        if not confirm:
            typer.confirm(f"Delete loan #{loan_id}?", abort=True)
        delete_loan(conn, loan_id)
        conn.commit()
        console.print(f"[green]Loan #{loan_id} deleted.[/]")
    finally:
        conn.close()


@loans_app.command("schedule")
def loan_schedule(
    loan_id: int = typer.Argument(..., help="Loan ID from `fintrack assets loan list`"),
    rows:    int = typer.Option(24, "--rows", "-n", help="Number of rows to show"),
):
    """Show amortization schedule for a loan."""
    from .assets.db import get_loans
    from .assets.loans import from_db_row, amortization_schedule

    conn = _open_db()
    try:
        all_loans = {r["id"]: r for r in get_loans(conn)}
        if loan_id not in all_loans:
            console.print(f"[red]Loan #{loan_id} not found.[/]")
            raise typer.Exit(1)
        loan = from_db_row(all_loans[loan_id])
        schedule = amortization_schedule(loan)

        t = Table(title=f"Amortization: {loan.name} (first {rows} payments)")
        t.add_column("#", justify="right"); t.add_column("Date")
        t.add_column("Payment", justify="right")
        t.add_column("Principal", justify="right")
        t.add_column("Interest", justify="right")
        t.add_column("Balance", justify="right")
        for row in schedule[:rows]:
            t.add_row(
                str(row.payment_number), row.payment_date.isoformat(),
                f"${row.payment:,.2f}", f"${row.principal_paid:,.2f}",
                f"${row.interest_paid:,.2f}", f"${row.balance:,.2f}",
            )
        console.print(t)
    finally:
        conn.close()


@loans_app.command("set-balance")
def loan_set_balance(
    loan_id: int   = typer.Argument(..., help="Loan ID from loan list"),
    balance: float = typer.Option(..., "--balance", "-b", help="Actual balance from your lender's portal"),
):
    """
    Override the calculated balance with the real figure from your lender.

    Use this when the scheduled amortization doesn't match reality — missed
    payments, extra principal payments, fees, or a loan you entered mid-term.
    The override shows in loan list with the scheduled balance alongside it
    for reference. Run clear-balance to revert to calculated.
    """
    from .assets.db import set_loan_balance, get_loans
    conn = _open_db()
    try:
        loans = {r["id"]: r for r in get_loans(conn)}
        if loan_id not in loans:
            console.print(f"[red]Loan #{loan_id} not found.[/]")
            raise typer.Exit(1)
        set_loan_balance(conn, loan_id, balance)
        conn.commit()
        console.print(f"[green]{loans[loan_id]['name']} balance set to ${balance:,.2f}[/]")
        console.print("  [dim]Scheduled amortization balance still shown for reference in loan list.[/]")
    finally:
        conn.close()


@loans_app.command("clear-balance")
def loan_clear_balance(
    loan_id: int = typer.Argument(..., help="Loan ID from loan list"),
):
    """Remove the balance override and revert to calculated amortization balance."""
    from .assets.db import clear_loan_balance, get_loans
    conn = _open_db()
    try:
        loans = {r["id"]: r for r in get_loans(conn)}
        if loan_id not in loans:
            console.print(f"[red]Loan #{loan_id} not found.[/]")
            raise typer.Exit(1)
        clear_loan_balance(conn, loan_id)
        conn.commit()
        console.print(f"[green]{loans[loan_id]['name']} reverted to calculated balance.[/]")
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# assets vehicle
# ══════════════════════════════════════════════════════════════════════════════

@vehicles_app.command("add")
def vehicle_add(
    name:        str   = typer.Option(..., "--name",  "-n", help="e.g. '2022 Honda CR-V'"),
    price:       float = typer.Option(..., "--price", "-p", help="Purchase price"),
    date_:       str   = typer.Option(..., "--date",  "-d", help="Purchase date YYYY-MM-DD"),
    depreciation: float = typer.Option(18.0, "--rate", help="Annual depreciation % (default 18%)"),
):
    """Add a vehicle for depreciation tracking."""
    from .assets.db import add_vehicle
    conn = _open_db()
    try:
        vid = add_vehicle(conn, name, price, date_, depreciation / 100)
        conn.commit()
        console.print(f"[green]Added vehicle #{vid}: {name}[/]")
        console.print(f"  ${price:,.2f} purchased {date_}, {depreciation:.0f}%/yr depreciation")
    finally:
        conn.close()


@vehicles_app.command("list")
def vehicle_list():
    """List vehicles with current estimated values."""
    from .assets.db import get_vehicles
    from .assets.vehicles import from_db_row, estimated_value, total_depreciation

    conn = _open_db()
    try:
        rows = get_vehicles(conn)
        if not rows:
            console.print("[yellow]No vehicles. Add one with [bold]fintrack assets vehicle add[/].[/]")
            return
        t = Table(title="Vehicles")
        t.add_column("ID"); t.add_column("Name")
        t.add_column("Purchased"); t.add_column("Original", justify="right")
        t.add_column("Est. Value", justify="right")
        t.add_column("Lost", justify="right")
        t.add_column("Rate", justify="right")
        for row in rows:
            v = from_db_row(row)
            val = estimated_value(v)
            lost = total_depreciation(v)
            t.add_row(
                str(v.id), v.name, v.purchase_date.isoformat(),
                f"${v.purchase_price:,.2f}", f"${val:,.2f}", f"${lost:,.2f}",
                f"{v.annual_depreciation*100:.0f}%/yr",
            )
        console.print(t)
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# assets property
# ══════════════════════════════════════════════════════════════════════════════

@property_app.command("add")
def property_add(
    name:           str   = typer.Option(...,   "--name",     "-n", help="e.g. 'Primary Residence'"),
    address:        str   = typer.Option(...,   "--address",  "-a", help="Full street address"),
    purchase_price: float = typer.Option(...,   "--price",    "-p", help="Purchase price"),
    purchase_date:  str   = typer.Option(...,   "--date",     "-d", help="Purchase date YYYY-MM-DD"),
    current_value:  Optional[float] = typer.Option(None, "--value", "-v",
                                                   help="Current market value (optional — set later with set-value)"),
):
    """Add a property. Market value can be set now or updated later."""
    from .assets.db import add_property
    conn = _open_db()
    try:
        pid = add_property(conn, name, address, purchase_price, purchase_date, current_value)
        conn.commit()
        console.print(f"[green]Added property #{pid}: {name}[/]")
        if current_value:
            console.print(f"  Value: ${current_value:,.0f}")
        else:
            console.print(
                "  [dim]No current value set. Run [bold]fintrack assets property set-value[/] "
                "or [bold]fintrack assets property refresh[/] to add one.[/]"
            )
    finally:
        conn.close()


@property_app.command("list")
def property_list():
    """List properties with current values and appreciation."""
    from .assets.db import get_properties
    from .assets.properties import from_db_row, appreciation, appreciation_pct

    conn = _open_db()
    try:
        rows = get_properties(conn)
        if not rows:
            console.print("[yellow]No properties. Add one with [bold]fintrack assets property add[/].[/]")
            return
        t = Table(title="Properties")
        t.add_column("ID"); t.add_column("Name"); t.add_column("Address")
        t.add_column("Purchased"); t.add_column("Purchase Price", justify="right")
        t.add_column("Est. Value", justify="right"); t.add_column("Appreciation", justify="right")
        t.add_column("Range", justify="right"); t.add_column("Updated")
        for row in rows:
            p = from_db_row(row)
            val_str = f"${p.current_value:,.0f}" if p.current_value else "[yellow]not set[/]"
            app = appreciation(p)
            app_str = (f"[green]+${app:,.0f} ({appreciation_pct(p):.1f}%)[/]" if app and app >= 0
                       else f"[red]-${abs(app):,.0f}[/]" if app else "[dim]—[/]")
            rng_str = (f"${p.value_range_low:,.0f}–${p.value_range_high:,.0f}"
                       if p.value_range_low and p.value_range_high else "[dim]—[/]")
            upd_str = p.value_updated_at[:10] if p.value_updated_at else "[dim]never[/]"
            t.add_row(str(p.id), p.name, p.address[:30], p.purchase_date.isoformat(),
                      f"${p.purchase_price:,.0f}", val_str, app_str, rng_str, upd_str)
        console.print(t)
    finally:
        conn.close()


@property_app.command("set-value")
def property_set_value(
    property_id:    int   = typer.Argument(..., help="Property ID from property list"),
    value:          float = typer.Option(..., "--value", "-v", help="Current market value"),
    range_low:      Optional[float] = typer.Option(None, "--low",  help="Low end of estimate range"),
    range_high:     Optional[float] = typer.Option(None, "--high", help="High end of estimate range"),
):
    """Manually set the current market value (e.g. from Zillow or Redfin)."""
    from .assets.db import set_property_value, get_property
    conn = _open_db()
    try:
        prop = get_property(conn, property_id)
        if not prop:
            console.print(f"[red]Property #{property_id} not found.[/]")
            raise typer.Exit(1)
        set_property_value(conn, property_id, value, range_low, range_high)
        conn.commit()
        console.print(f"[green]{prop['name']} value updated to ${value:,.0f}[/]")
    finally:
        conn.close()


@property_app.command("refresh")
def property_refresh(
    property_id: Optional[int] = typer.Argument(None, help="Property ID, or omit to refresh all"),
):
    """
    Fetch current market value from Rentcast AVM API.

    Requires RENTCAST_API_KEY in .env. Each call uses one API request
    from your monthly quota (~50 on the free tier).
    """
    from .assets.db import get_properties, get_property, set_property_value
    from .assets.properties import from_db_row, fetch_rentcast_value

    s = get_settings()
    if not s.rentcast_api_key:
        console.print(
            "[red]RENTCAST_API_KEY not set in .env.[/]\n"
            "Sign up for a free key at [link]https://www.rentcast.io[/link] "
            "then add it to your .env file."
        )
        raise typer.Exit(1)

    conn = _open_db()
    try:
        rows = [get_property(conn, property_id)] if property_id else get_properties(conn)
        rows = [r for r in rows if r is not None]

        if not rows:
            console.print("[yellow]No properties found.[/]")
            return

        for row in rows:
            prop = from_db_row(row)
            console.print(f"Fetching estimate for [bold]{prop.name}[/] ({prop.address})...")
            try:
                result = fetch_rentcast_value(prop.address, s.rentcast_api_key)
                set_property_value(
                    conn, prop.id,
                    result["price"],
                    result.get("price_range_low"),
                    result.get("price_range_high"),
                )
                conn.commit()
                low  = result.get("price_range_low")
                high = result.get("price_range_high")
                rng  = f"  Range: ${low:,.0f}–${high:,.0f}" if low and high else ""
                console.print(f"  [green]${result['price']:,.0f}[/]{rng}")
            except ValueError as e:
                console.print(f"  [red]Failed: {e}[/]")
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# assets equity
# ══════════════════════════════════════════════════════════════════════════════

@equity_app.command("add-rsu")
def equity_add_rsu(
    ticker:    str   = typer.Option(..., "--ticker", "-t"),
    date_:     str   = typer.Option(..., "--date",   "-d", help="Grant date YYYY-MM-DD"),
    shares:    float = typer.Option(..., "--shares",  "-s", help="Total shares granted"),
    cliff:     int   = typer.Option(12,  "--cliff",        help="Cliff in months (default 12)"),
    vest:      int   = typer.Option(48,  "--vest",         help="Total vest period in months (default 48)"),
    freq:      str   = typer.Option("monthly", "--freq",   help="monthly|quarterly|annual"),
):
    """Add an RSU grant."""
    from .assets.db import add_rsu_grant
    conn = _open_db()
    try:
        gid = add_rsu_grant(conn, ticker, date_, shares, cliff, vest, freq)
        conn.commit()
        console.print(f"[green]Added RSU grant #{gid}: {shares:.0f} {ticker.upper()} shares[/]")
        console.print(f"  Grant: {date_}  |  Cliff: {cliff}mo  |  Total: {vest}mo  |  {freq}")
    finally:
        conn.close()


@equity_app.command("add-espp")
def equity_add_espp(
    ticker:        str   = typer.Option("CSCO",  "--ticker",       "-t"),
    offering_start: str  = typer.Option(...,     "--start",        "-s", help="Offering start date YYYY-MM-DD"),
    contribution:  float = typer.Option(10.0,    "--contribution", "-c", help="Contribution % of paycheck (default 10%)"),
    discount:      float = typer.Option(15.0,    "--discount",     "-d", help="Discount % (default 15%)"),
    period:        int   = typer.Option(6,       "--period",             help="Purchase period months (default 6)"),
    lookback:      int   = typer.Option(24,      "--lookback",           help="Lookback months for lower-of calc (default 24 — verify your plan docs)"),
):
    """
    Add an ESPP plan.

    IMPORTANT: verify --lookback against your plan documents. Standard Cisco ESPP
    uses 24 months (offering period). Some interpret it as 6 (purchase period only).
    """
    from .assets.db import add_espp_plan
    conn = _open_db()
    try:
        gid = add_espp_plan(
            conn, ticker, offering_start,
            contribution / 100, discount / 100, period, lookback,
        )
        conn.commit()
        console.print(f"[green]Added ESPP plan #{gid}: {ticker.upper()}[/]")
        console.print(
            f"  {contribution:.0f}% contribution  |  {discount:.0f}% discount  "
            f"|  {period}-month periods  |  {lookback}-month lookback"
        )
        if lookback == 24:
            console.print("  [dim]Verify lookback_months=24 matches your plan documents.[/]")
    finally:
        conn.close()


@equity_app.command("list")
def equity_list():
    """Show all equity grants with vested shares and current values."""
    from .assets.db import get_equity_grants
    from .assets.equity import rsu_from_db, espp_from_db, vested_shares, next_vest_date, shares_at_next_vest
    from .assets.prices import get_current_price

    conn = _open_db()
    try:
        grants = get_equity_grants(conn)
        if not grants:
            console.print("[yellow]No grants. Add with [bold]fintrack assets equity add-rsu[/] or [bold]add-espp[/].[/]")
            return

        rsus = [g for g in grants if g["grant_type"] == "rsu"]
        espps = [g for g in grants if g["grant_type"] == "espp"]

        if rsus:
            t = Table(title="RSU Grants")
            t.add_column("ID"); t.add_column("Ticker"); t.add_column("Grant Date")
            t.add_column("Total", justify="right"); t.add_column("Vested", justify="right")
            t.add_column("Unvested", justify="right"); t.add_column("Price", justify="right")
            t.add_column("Vested Value", justify="right"); t.add_column("Next Vest")

            for row in rsus:
                grant = rsu_from_db(row)
                vest = vested_shares(grant)
                unvest = grant.total_shares - vest
                nv = next_vest_date(grant)
                nv_shares = shares_at_next_vest(grant)
                try:
                    price = get_current_price(grant.ticker, conn)
                    price_str = f"${price:,.2f}"
                    value_str = f"${vest * price:,.2f}"
                except Exception:
                    price_str = "[dim]n/a[/]"
                    value_str = "[dim]n/a[/]"
                nv_str = f"{nv} (+{nv_shares:.0f})" if nv else "[dim]fully vested[/]"
                t.add_row(
                    str(grant.id), grant.ticker, grant.grant_date.isoformat(),
                    f"{grant.total_shares:.0f}", f"{vest:.2f}", f"{unvest:.2f}",
                    price_str, value_str, nv_str,
                )
            console.print(t)

        if espps:
            t = Table(title="ESPP Plans")
            t.add_column("ID"); t.add_column("Ticker"); t.add_column("Offering Start")
            t.add_column("Contribution"); t.add_column("Discount"); t.add_column("Lookback")
            for row in espps:
                plan = espp_from_db(row)
                t.add_row(
                    str(plan.id), plan.ticker, plan.offering_start_date.isoformat(),
                    f"{plan.contribution_rate*100:.0f}%",
                    f"{plan.discount_rate*100:.0f}%",
                    f"{plan.lookback_months}mo",
                )
            console.print(t)

    finally:
        conn.close()


@equity_app.command("record")
def equity_record(
    grant_id:  int   = typer.Option(...,    "--grant",  "-g", help="Grant ID from equity list"),
    txn_type:  str   = typer.Option(...,    "--type",   "-t", help="vest|sell|espp_purchase"),
    txn_date:  str   = typer.Option(...,    "--date",   "-d", help="Transaction date YYYY-MM-DD"),
    shares:    float = typer.Option(...,    "--shares", "-s"),
    price:     float = typer.Option(...,    "--price",  "-p", help="Price per share"),
    notes:     Optional[str] = typer.Option(None, "--notes"),
):
    """Record a vest event, stock sale, or ESPP purchase."""
    from .assets.db import record_equity_transaction
    conn = _open_db()
    try:
        tid = record_equity_transaction(conn, grant_id, txn_type, txn_date, shares, price, notes)
        conn.commit()
        gross = shares * price
        console.print(f"[green]Recorded #{tid}: {txn_type} — {shares:.4f} shares @ ${price:.4f} = ${gross:,.2f}[/]")
    finally:
        conn.close()


@equity_app.command("scan")
def equity_scan(
    days:      int   = typer.Option(30,    "--days",      "-d", help="Look back N days (default 30)"),
    min_amount: float = typer.Option(500.0, "--min",      "-m", help="Minimum transaction amount"),
):
    """
    Scan recent brokerage transactions for potential stock sales.

    Shows candidates for you to review. Use `fintrack assets equity record`
    to log confirmed sales.
    """
    from .assets.db import get_equity_grants
    from .assets.equity import find_potential_sales

    conn = _open_db()
    try:
        grants = get_equity_grants(conn)
        tickers = list({g["ticker"] for g in grants})

        candidates = find_potential_sales(conn, tickers, days, min_amount)
        if not candidates:
            console.print(f"[green]No potential stock sales found in the last {days} days.[/]")
            return

        console.print(f"\nFound [bold]{len(candidates)}[/] potential sale(s) to review:\n")
        t = Table(title=f"Potential Stock Sales (last {days} days)")
        t.add_column("Date"); t.add_column("Description")
        t.add_column("Amount", justify="right")
        t.add_column("Institution"); t.add_column("Ticker Match")
        t.add_column("Confidence")

        for c in candidates:
            ticker_str = f"[green]{c['matched_ticker']}[/]" if c["matched_ticker"] else "[dim]unknown[/]"
            conf_str = f"[green]{c['confidence']}[/]" if c["confidence"] == "high" else f"[yellow]{c['confidence']}[/]"
            t.add_row(
                c["date"], (c["description"] or "")[:40],
                f"${c['amount']:,.2f}", c["institution"],
                ticker_str, conf_str,
            )
        console.print(t)
        console.print("\nTo record a confirmed sale:")
        console.print("  [bold]fintrack assets equity record --grant <id> --type sell --date <date> --shares <n> --price <p>[/]\n")
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# assets equity — direct holdings (any source)
# ══════════════════════════════════════════════════════════════════════════════

@equity_app.command("add-holding")
def equity_add_holding(
    ticker:     str            = typer.Option(...,  "--ticker", "-t", help="Stock ticker (e.g. AAPL)"),
    shares:     float          = typer.Option(...,  "--shares", "-s", help="Number of shares currently owned"),
    date_:      str            = typer.Option(...,  "--date",   "-d", help="Date of this snapshot YYYY-MM-DD"),
    cost_basis: Optional[float] = typer.Option(None, "--cost-basis", "-c",
                                               help="Average cost per share (optional, for reference only)"),
    notes:      Optional[str]  = typer.Option(None, "--notes",  "-n",
                                              help="Where these shares came from, e.g. 'Schwab brokerage'"),
):
    """
    Record shares you already own from any source.

    Use this for shares bought directly, inherited, transferred from another
    broker, or any position that didn't come through an RSU grant or ESPP.
    Update with `fintrack assets equity update-holding` whenever the count changes.
    """
    from .assets.db import add_holding
    conn = _open_db()
    try:
        hid = add_holding(conn, ticker, shares, date_, cost_basis, notes)
        conn.commit()
        console.print(f"[green]Added holding #{hid}: {shares:g} shares of {ticker.upper()}[/]")
        if cost_basis:
            console.print(f"  Cost basis: ${cost_basis:.2f}/share  (total basis: ${shares * cost_basis:,.2f})")
        if notes:
            console.print(f"  Source: {notes}")
    finally:
        conn.close()


@equity_app.command("list-holdings")
def equity_list_holdings():
    """List all directly-entered stock holdings."""
    from .assets.db import get_holdings
    from .assets.prices import get_current_price

    conn = _open_db()
    try:
        rows = get_holdings(conn)
        if not rows:
            console.print("[yellow]No direct holdings. Add one with [bold]fintrack assets equity add-holding[/].[/]")
            return

        t = Table(title="Direct Stock Holdings")
        t.add_column("ID"); t.add_column("Ticker")
        t.add_column("Shares", justify="right")
        t.add_column("Cost Basis", justify="right")
        t.add_column("Current Price", justify="right")
        t.add_column("Market Value", justify="right")
        t.add_column("Gain/Loss", justify="right")
        t.add_column("As Of"); t.add_column("Notes")

        for row in rows:
            ticker = row["ticker"]
            shares = row["shares"]
            cost   = row.get("cost_basis")
            try:
                price = get_current_price(ticker, conn)
                price_str = f"${price:,.2f}"
                value = shares * price
                value_str = f"${value:,.2f}"
                if cost:
                    gain = value - shares * cost
                    color = "green" if gain >= 0 else "red"
                    sign  = "+" if gain >= 0 else ""
                    gain_str = f"[{color}]{sign}${gain:,.2f}[/]"
                else:
                    gain_str = "[dim]—[/]"
            except Exception:
                price_str = value_str = gain_str = "[dim]n/a[/]"

            cost_str = f"${cost:.2f}" if cost else "[dim]—[/]"
            t.add_row(
                str(row["id"]), ticker, f"{shares:g}",
                cost_str, price_str, value_str, gain_str,
                row["as_of_date"], (row.get("notes") or "")[:30],
            )
        console.print(t)
    finally:
        conn.close()


@equity_app.command("update-holding")
def equity_update_holding(
    holding_id:  int            = typer.Argument(..., help="Holding ID from list-holdings"),
    shares:      Optional[float] = typer.Option(None, "--shares", "-s", help="New share count"),
    cost_basis:  Optional[float] = typer.Option(None, "--cost-basis", "-c"),
    date_:       Optional[str]   = typer.Option(None, "--date", "-d", help="Updated as-of date YYYY-MM-DD"),
    notes:       Optional[str]   = typer.Option(None, "--notes", "-n"),
):
    """Update shares, cost basis, or notes on a direct holding."""
    from .assets.db import update_holding
    conn = _open_db()
    try:
        update_holding(conn, holding_id, shares, cost_basis, date_, notes)
        conn.commit()
        console.print(f"[green]Holding #{holding_id} updated.[/]")
    finally:
        conn.close()


@equity_app.command("remove-holding")
def equity_remove_holding(
    holding_id: int = typer.Argument(..., help="Holding ID from list-holdings"),
):
    """Remove a direct holding entry."""
    from .assets.db import delete_holding
    conn = _open_db()
    try:
        delete_holding(conn, holding_id)
        conn.commit()
        console.print(f"[green]Holding #{holding_id} removed.[/]")
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# assets account  (401k, IRA, HSA, pension, etc.)
# ══════════════════════════════════════════════════════════════════════════════

@account_app.command("add")
def account_add(
    name:        str            = typer.Option(..., "--name",        "-n"),
    type_:       str            = typer.Option(..., "--type",        "-t",
                                               help="401k | roth_ira | traditional_ira | hsa | pension | brokerage | other"),
    institution: Optional[str]  = typer.Option(None, "--institution", "-i", help="e.g. Fidelity, Vanguard"),
    balance:     Optional[float] = typer.Option(None, "--balance",   "-b", help="Current balance"),
    notes:       Optional[str]  = typer.Option(None, "--notes"),
):
    """Add a retirement or other manually-tracked account."""
    from .assets.db import add_manual_account, ACCOUNT_TYPES
    if type_ not in ACCOUNT_TYPES:
        console.print(f"[red]Unknown type '{type_}'. Valid types: {', '.join(ACCOUNT_TYPES)}[/]")
        raise typer.Exit(1)
    conn = _open_db()
    try:
        aid = add_manual_account(conn, name, type_, institution, balance, notes)
        conn.commit()
        console.print(f"[green]Added account #{aid}: {name} ({type_})[/]")
        if balance is not None:
            console.print(f"  Balance: ${balance:,.2f}")
        else:
            console.print("  [dim]No balance set. Run [bold]fintrack assets account set-balance[/] to add one.[/]")
    finally:
        conn.close()


@account_app.command("list")
def account_list():
    """List all manually-tracked accounts with current balances."""
    from .assets.db import get_manual_accounts
    conn = _open_db()
    try:
        rows = get_manual_accounts(conn)
        if not rows:
            console.print("[yellow]No accounts. Add one with [bold]fintrack assets account add[/].[/]")
            return
        t = Table(title="Retirement & Other Accounts")
        t.add_column("ID"); t.add_column("Name"); t.add_column("Type")
        t.add_column("Institution"); t.add_column("Balance", justify="right")
        t.add_column("Last Updated"); t.add_column("Notes")
        total = 0.0
        for row in rows:
            bal = row.get("balance")
            bal_str = f"${bal:,.2f}" if bal is not None else "[yellow]not set[/]"
            upd_str = row["balance_updated_at"][:10] if row.get("balance_updated_at") else "[dim]never[/]"
            if bal:
                total += bal
            t.add_row(str(row["id"]), row["name"], row["account_type"],
                      row.get("institution") or "", bal_str, upd_str,
                      (row.get("notes") or "")[:30])
        console.print(t)
        console.print(f"  Total: [bold]${total:,.2f}[/]")
    finally:
        conn.close()


@account_app.command("set-balance")
def account_set_balance(
    account_id: int   = typer.Argument(..., help="Account ID from account list"),
    balance:    float = typer.Option(..., "--balance", "-b", help="Current balance"),
):
    """Update the balance on a manually-tracked account."""
    from .assets.db import set_account_balance, get_manual_accounts
    conn = _open_db()
    try:
        accounts = {r["id"]: r for r in get_manual_accounts(conn)}
        if account_id not in accounts:
            console.print(f"[red]Account #{account_id} not found.[/]")
            raise typer.Exit(1)
        set_account_balance(conn, account_id, balance)
        conn.commit()
        console.print(f"[green]{accounts[account_id]['name']} updated to ${balance:,.2f}[/]")
    finally:
        conn.close()


@account_app.command("remove")
def account_remove(
    account_id: int = typer.Argument(..., help="Account ID from account list"),
):
    """Remove a manually-tracked account."""
    from .assets.db import delete_manual_account
    conn = _open_db()
    try:
        delete_manual_account(conn, account_id)
        conn.commit()
        console.print(f"[green]Account #{account_id} removed.[/]")
    finally:
        conn.close()


# ── fintrack adjust ──────────────────────────────────────────────────────────
# Saved budget normalizations: annual→monthly conversions, known one-time items,
# etc.  These auto-apply on every `fintrack budget` run.
#
# Examples:
#   fintrack adjust add "Summer camp annual /12" -1000 --category FOOD_AND_DRINK
#   fintrack adjust add "Motorsport Reg annual"   -460 --category ENTERTAINMENT
#   fintrack adjust list
#   fintrack adjust rm 3
#   fintrack adjust off 2   (keep the entry but disable it temporarily)
#   fintrack adjust on  2

@adjust_app.command("add")
def adjust_add(
    label:   str   = typer.Argument(..., help="Description of the adjustment"),
    amount:  float = typer.Option(..., "--amount", "-a",
                        help="Monthly delta (negative = reduces expense). "
                             "Always use --amount / -a so negative values are "
                             "not mistaken for option flags."),
    category: Optional[str] = typer.Option(
        None, "--category", "-c",
        help="Variable spending category this offsets (e.g. FOOD_AND_DRINK). "
             "Display-only; does not affect the math.",
    ),
    notes: Optional[str] = typer.Option(
        None, "--notes", "-n", help="Free-text explanation (optional).",
    ),
):
    """
    Save a named normalization so it auto-applies on every budget run.

    Use negative amounts to remove inflated spending (annual items, one-time
    purchases that distort a 3-month average).  Always pass the amount via
    --amount / -a so the leading minus sign is not parsed as an option flag:

      fintrack adjust add "Echo Hill summer camp /12" -a -1000 -c CHILDCARE
      fintrack adjust add "Motorsport Reg annual"     -a  -460 -c ENTERTAINMENT
      fintrack adjust add "Amazon Prime annual /12"   -a  -138
    """
    from .db import add_budget_adjustment
    conn = _open_db()
    try:
        adj_id = add_budget_adjustment(conn, label, amount, category, notes)
        conn.commit()
        sign  = "+" if amount > 0 else ""
        color = "red" if amount > 0 else "green"
        console.print(
            f"[green]Saved[/] adjustment [dim][{adj_id}][/] "
            f"[bold]{label}[/]: [{color}]{sign}${amount:,.2f}/mo[/]"
        )
    finally:
        conn.close()


@adjust_app.command("list")
def adjust_list(
    all_items: bool = typer.Option(False, "--all", "-a",
                                   help="Show disabled items too"),
):
    """List all saved budget normalizations."""
    from .db import get_budget_adjustments
    conn = _open_db()
    try:
        rows = get_budget_adjustments(conn, active_only=not all_items)
        if not rows:
            console.print("[dim]No saved adjustments yet.  Use [bold]fintrack adjust add[/] to create one.[/]")
            return
        table = Table(title="Saved Budget Adjustments", box=None, show_header=True)
        table.add_column("ID",       justify="right", style="dim")
        table.add_column("Label",    justify="left")
        table.add_column("Monthly",  justify="right")
        table.add_column("Category", justify="left", style="dim")
        table.add_column("Notes",    justify="left", style="dim")
        table.add_column("Active",   justify="center")
        net = 0.0
        for r in rows:
            amt   = r["monthly_amount"]
            sign  = "+" if amt > 0 else ""
            color = "red" if amt > 0 else "green"
            net  += amt
            active_str = "[green]yes[/]" if r["active"] else "[dim]no[/]"
            table.add_row(
                str(r["id"]),
                r["label"],
                f"[{color}]{sign}${amt:,.2f}/mo[/]",
                r["category"] or "",
                r["notes"] or "",
                active_str,
            )
        net_sign  = "+" if net > 0 else ""
        net_color = "red" if net > 0 else "green"
        table.add_row(
            "", "[bold]Net[/]",
            f"[bold {net_color}]{net_sign}${net:,.2f}/mo[/]",
            "", "", "",
        )
        console.print(table)
    finally:
        conn.close()


@adjust_app.command("rm")
def adjust_rm(
    adjustment_id: int = typer.Argument(..., help="ID from 'fintrack adjust list'"),
):
    """Permanently delete a saved adjustment."""
    from .db import remove_budget_adjustment
    conn = _open_db()
    try:
        ok = remove_budget_adjustment(conn, adjustment_id)
        conn.commit()
        if ok:
            console.print(f"[green]Removed adjustment #{adjustment_id}.[/]")
        else:
            console.print(f"[yellow]No adjustment with ID {adjustment_id}.[/]")
    finally:
        conn.close()


@adjust_app.command("off")
def adjust_off(
    adjustment_id: int = typer.Argument(..., help="ID from 'fintrack adjust list'"),
):
    """Disable a saved adjustment without deleting it (useful for seasonal items)."""
    from .db import toggle_budget_adjustment
    conn = _open_db()
    try:
        ok = toggle_budget_adjustment(conn, adjustment_id, active=False)
        conn.commit()
        if ok:
            console.print(f"[dim]Adjustment #{adjustment_id} disabled. Re-enable with [bold]fintrack adjust on {adjustment_id}[/].[/]")
        else:
            console.print(f"[yellow]No adjustment with ID {adjustment_id}.[/]")
    finally:
        conn.close()


@adjust_app.command("on")
def adjust_on(
    adjustment_id: int = typer.Argument(..., help="ID from 'fintrack adjust list'"),
):
    """Re-enable a previously disabled adjustment."""
    from .db import toggle_budget_adjustment
    conn = _open_db()
    try:
        ok = toggle_budget_adjustment(conn, adjustment_id, active=True)
        conn.commit()
        if ok:
            console.print(f"[green]Adjustment #{adjustment_id} enabled.[/]")
        else:
            console.print(f"[yellow]No adjustment with ID {adjustment_id}.[/]")
    finally:
        conn.close()


# ── fintrack target ──────────────────────────────────────────────────────────
# Per-category monthly spending targets shown alongside actuals in the budget.
#
# Examples:
#   fintrack target set FOOD_AND_DRINK 800
#   fintrack target set ENTERTAINMENT  200  --notes "racing only a few times/year"
#   fintrack target list
#   fintrack target rm FOOD_AND_DRINK

@target_app.command("set")
def target_set(
    category:      str   = typer.Argument(..., help="Category primary name (e.g. FOOD_AND_DRINK)"),
    target_amount: float = typer.Argument(..., help="Monthly spending target ($)"),
    notes: Optional[str] = typer.Option(None, "--notes", "-n", help="Optional reminder note"),
):
    """
    Set a monthly spending target for a category.

    The target appears in the Variable Spending section of 'fintrack budget',
    showing whether you are over or under each month.

      fintrack target set FOOD_AND_DRINK 800
      fintrack target set ENTERTAINMENT  200
      fintrack target set GENERAL_MERCHANDISE 300

    Run 'fintrack drill CATEGORY' to see a breakdown before deciding on a target.
    """
    from .db import set_budget_target
    conn = _open_db()
    try:
        set_budget_target(conn, category.upper(), target_amount, notes)
        conn.commit()
        console.print(
            f"[green]Target set:[/] [bold]{category.upper()}[/] → "
            f"[bold]${target_amount:,.2f}/mo[/]"
            + (f"  [dim]({notes})[/]" if notes else "")
        )
    finally:
        conn.close()


@target_app.command("list")
def target_list():
    """Show all category spending targets."""
    from .db import get_budget_targets
    conn = _open_db()
    try:
        targets = get_budget_targets(conn)
        if not targets:
            console.print("[dim]No targets set yet.  Use [bold]fintrack target set CATEGORY AMOUNT[/].[/]")
            return
        table = Table(title="Spending Targets", box=None)
        table.add_column("Category",  justify="left")
        table.add_column("Target",    justify="right")
        table.add_column("Notes",     justify="left", style="dim")
        table.add_column("Updated",   justify="left", style="dim")
        for cat, t in targets.items():
            table.add_row(
                cat,
                f"${t['target_amount']:,.2f}/mo",
                t["notes"] or "",
                t["updated_at"],
            )
        console.print(table)
    finally:
        conn.close()


@target_app.command("rm")
def target_rm(
    category: str = typer.Argument(..., help="Category to remove the target for"),
):
    """Remove a category spending target."""
    from .db import remove_budget_target
    conn = _open_db()
    try:
        ok = remove_budget_target(conn, category.upper())
        conn.commit()
        if ok:
            console.print(f"[green]Target for {category.upper()} removed.[/]")
        else:
            console.print(f"[yellow]No target found for {category.upper()}.[/]")
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# fintrack tax tag
# ══════════════════════════════════════════════════════════════════════════════

TAX_CATEGORY_CHOICES = [
    "medical", "hsa_fsa", "charitable", "dependent_care", "education",
    "self_employed", "home_office", "energy_credit", "mortgage_interest",
    "investment", "alimony_paid", "state_local_tax", "estimated_tax", "other",
]


@tax_tag_app.callback(invoke_without_command=True)
def tax_tag_add(
    ctx: typer.Context,
    transaction_id: Optional[str] = typer.Argument(
        None, help="Transaction ID to tag (from report spending or the DB)."
    ),
    category: Optional[str] = typer.Option(
        None, "--category", "-c",
        help=f"Tax category: {', '.join(TAX_CATEGORY_CHOICES)}",
    ),
    note: Optional[str] = typer.Option(
        None, "--note", "-n",
        help="Optional note, e.g. 'summer camp for son', 'vision exam + glasses'.",
    ),
    year: Optional[int] = typer.Option(
        None, "--year", "-y",
        help="Override tax year (default: taken from transaction date).",
    ),
):
    """
    Tag a transaction with a tax category so it appears in 'fintrack report tax-summary'.

      fintrack tax tag txn-abc123 --category dependent_care --note "echo hill summer camp"
      fintrack tax tag txn-xyz789 --category medical         --note "vision exam + glasses"
      fintrack tax tag txn-def456 --category charitable      --note "Red Cross donation"

    Run 'fintrack tax tag list' to review all tagged transactions.
    Run 'fintrack report tax-summary --year 2025' for a year-end rollup.
    """
    if ctx.invoked_subcommand is not None:
        return

    if not transaction_id:
        console.print(ctx.get_help())
        raise typer.Exit(0)

    if not category:
        console.print(f"[red]--category is required. Choices: {', '.join(TAX_CATEGORY_CHOICES)}[/]")
        raise typer.Exit(1)

    if category not in TAX_CATEGORY_CHOICES:
        console.print(
            f"[red]Unknown category '{category}'. "
            f"Valid choices: {', '.join(TAX_CATEGORY_CHOICES)}[/]"
        )
        raise typer.Exit(1)

    from .db import add_tax_tag

    conn = _open_db()
    try:
        txn = conn.execute(
            """
            SELECT t.transaction_id, t.date, t.amount,
                   COALESCE(t.merchant_name, t.raw_name, 'Unknown') AS merchant
            FROM transactions t
            WHERE t.transaction_id = ?
            """,
            (transaction_id,),
        ).fetchone()

        if not txn:
            console.print(f"[red]Transaction '{transaction_id}' not found.[/]")
            raise typer.Exit(1)

        tax_year = year if year is not None else int(txn["date"][:4])
        tag_id = add_tax_tag(conn, transaction_id, category, tax_year, note)
        conn.commit()

        console.print(
            f"[green]Tagged[/] [bold]{txn['merchant']}[/] "
            f"(${txn['amount']:,.2f} on {txn['date']}) "
            f"as [bold]{category}[/] for tax year [bold]{tax_year}[/] "
            f"[dim][tag #{tag_id}][/]"
        )
        if note:
            console.print(f"  Note: {note}")
    finally:
        conn.close()


@tax_tag_app.command("list")
def tax_tag_list(
    year: Optional[int] = typer.Option(None, "--year", "-y", help="Filter by tax year"),
    category: Optional[str] = typer.Option(None, "--category", "-c", help="Filter by tax category"),
):
    """Show all tax-tagged transactions."""
    from .db import get_tax_tags

    conn = _open_db()
    try:
        tags = get_tax_tags(conn, year=year, tax_category=category)
        if not tags:
            filter_str = ""
            if year:
                filter_str += f" for {year}"
            if category:
                filter_str += f" in {category}"
            console.print(
                f"[dim]No tax-tagged transactions{filter_str}. "
                f"Use [bold]fintrack tax tag <txn_id> --category <cat>[/] to add one.[/]"
            )
            return

        t = Table(title=f"Tax-Tagged Transactions ({len(tags)} total)")
        t.add_column("Tag #", justify="right", style="dim")
        t.add_column("Year", justify="right")
        t.add_column("Date")
        t.add_column("Merchant")
        t.add_column("Amount", justify="right")
        t.add_column("Category")
        t.add_column("Note", style="dim")

        for tag in tags:
            t.add_row(
                str(tag["tag_id"]),
                str(tag["tax_year"]),
                tag["date"],
                (tag["merchant"] or "")[:35],
                f"${tag['amount']:,.2f}",
                tag["tax_category"],
                (tag["note"] or "")[:40],
            )
        console.print(t)

        total = sum(t_["amount"] for t_ in tags)
        console.print(f"\n  Total tagged spend: [bold]${total:,.2f}[/]")
    finally:
        conn.close()


@tax_tag_app.command("rm")
def tax_tag_rm(
    tag_id: int = typer.Argument(..., help="Tag ID from 'fintrack tax tag list'"),
):
    """Remove a tax tag from a transaction."""
    from .db import remove_tax_tag

    conn = _open_db()
    try:
        ok = remove_tax_tag(conn, tag_id)
        conn.commit()
        if ok:
            console.print(f"[green]Tax tag #{tag_id} removed.[/]")
        else:
            console.print(f"[yellow]No tag with ID {tag_id}.[/]")
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# fintrack tax docs
# ══════════════════════════════════════════════════════════════════════════════

TAX_DOC_TYPE_CHOICES = [
    "W-2", "1099-INT", "1099-DIV", "1099-B", "1099-NEC",
    "1099-MISC", "1099-R", "1099-SA", "1098", "1098-E", "1098-T", "SSA-1099", "other",
]

# Account type/subtype → expected document types for a given institution.
_ACCOUNT_DOC_MAP: dict[str, list[str]] = {
    "investment": ["1099-DIV", "1099-B", "1099-INT"],
    "brokerage":  ["1099-DIV", "1099-B", "1099-INT"],
    "depository": ["1099-INT"],
    "mortgage":   ["1098"],   # matched against subtype
}


@tax_docs_app.command("list")
def tax_docs_list(
    year: Optional[int] = typer.Option(None, "--year", "-y", help="Filter by year (default: current year)"),
):
    """List expected and received tax documents."""
    from .db import get_tax_documents

    if year is None:
        year = date.today().year

    conn = _open_db()
    try:
        docs = get_tax_documents(conn, year=year)
        if not docs:
            console.print(
                f"[yellow]No documents tracked for {year}. "
                f"Run [bold]fintrack tax docs init --year {year}[/] to pre-populate "
                f"from linked institutions, or [bold]fintrack tax docs add[/] to add manually.[/]"
            )
            return

        import os
        received_count = sum(1 for d in docs if d["received"])
        t = Table(
            title=f"Tax Documents — {year}  "
                  f"({received_count}/{len(docs)} received)"
        )
        t.add_column("ID",          justify="right", style="dim")
        t.add_column("Institution")
        t.add_column("Doc Type")
        t.add_column("Received",    justify="center")
        t.add_column("Date",        style="dim")
        t.add_column("File",        style="dim")
        t.add_column("Notes",       style="dim")

        for d in docs:
            recv_str = "[green]yes[/]" if d["received"] else "[yellow]waiting[/]"
            fp = d.get("file_path")
            file_str = os.path.basename(fp) if fp else ""
            t.add_row(
                str(d["id"]),
                d["institution"],
                d["doc_type"],
                recv_str,
                (d["received_date"] or "")[:10],
                file_str[:35],
                (d["notes"] or "")[:30],
            )
        console.print(t)

        missing = [d for d in docs if not d["received"]]
        if missing:
            console.print(
                f"\n  [yellow]{len(missing)} document(s) not yet received.[/] "
                f"Run [bold]fintrack tax docs mark <id> --received[/] when they arrive."
            )
    finally:
        conn.close()


@tax_docs_app.command("add")
def tax_docs_add(
    year: int = typer.Option(..., "--year", "-y", help="Tax year (e.g. 2025)"),
    institution: str = typer.Option(..., "--institution", "-i", help="Institution name (e.g. 'Bank of America')"),
    doc_type: str = typer.Option(..., "--doc-type", "-d",
                                 help=f"Document type: {', '.join(TAX_DOC_TYPE_CHOICES)}"),
    notes: Optional[str] = typer.Option(None, "--notes", "-n"),
):
    """Add a tax document to track."""
    from .db import upsert_tax_document

    conn = _open_db()
    try:
        doc_id = upsert_tax_document(conn, year, institution, doc_type, notes)
        conn.commit()
        console.print(
            f"[green]Added[/] {doc_type} from [bold]{institution}[/] "
            f"for {year} [dim][#{doc_id}][/]"
        )
    finally:
        conn.close()


@tax_docs_app.command("mark")
def tax_docs_mark(
    doc_id: int = typer.Argument(..., help="Document ID from 'fintrack tax docs list'"),
    received: bool = typer.Option(True, "--received/--not-received",
                                  help="Mark as received (default) or not received"),
    received_date: Optional[str] = typer.Option(
        None, "--date", "-d",
        help="Date received YYYY-MM-DD (default: today)",
    ),
):
    """Mark a tax document as received (or not received)."""
    from .db import mark_tax_document_received, get_tax_document

    conn = _open_db()
    try:
        doc = get_tax_document(conn, doc_id)
        if not doc:
            console.print(f"[red]Document #{doc_id} not found.[/]")
            raise typer.Exit(1)

        recv_date = received_date or (date.today().isoformat() if received else None)
        mark_tax_document_received(conn, doc_id, received, recv_date)
        conn.commit()

        if received:
            console.print(
                f"[green]Marked received:[/] {doc['doc_type']} from [bold]{doc['institution']}[/] "
                f"({doc['year']})  [dim]{recv_date}[/]"
            )
        else:
            console.print(
                f"[yellow]Marked not received:[/] {doc['doc_type']} from {doc['institution']}"
            )
    finally:
        conn.close()


@tax_docs_app.command("init")
def tax_docs_init(
    year: int = typer.Option(..., "--year", "-y", help="Tax year to initialize (e.g. 2025)"),
):
    """
    Pre-populate expected tax documents from linked Plaid institutions.

    Looks at each linked institution's account types and suggests likely 1099s:
      - Depository accounts   → 1099-INT
      - Investment/brokerage  → 1099-DIV, 1099-B, 1099-INT
      - Mortgage loan         → 1098

    Run 'fintrack tax docs add' to add a W-2 from your employer manually,
    since employer info is not tracked in Plaid.
    """
    from .db import upsert_tax_document

    conn = _open_db()
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT i.institution_name, a.type, a.subtype
            FROM items i
            JOIN accounts a ON a.item_id = i.item_id
            ORDER BY i.institution_name
            """
        ).fetchall()

        if not rows:
            console.print("[yellow]No linked institutions found. Run [bold]fintrack link[/] first.[/]")
            return

        inserted = 0
        skipped  = 0
        seen: set[tuple[str, str]] = set()

        for row in rows:
            institution = row["institution_name"]
            acct_type   = (row["type"]    or "").lower()
            acct_sub    = (row["subtype"] or "").lower()

            doc_types: list[str] = []
            if acct_type in ("investment",) or acct_sub in ("brokerage", "investment"):
                doc_types = ["1099-DIV", "1099-B", "1099-INT"]
            elif acct_type == "depository":
                doc_types = ["1099-INT"]
            elif acct_sub == "mortgage":
                doc_types = ["1098"]

            for dt in doc_types:
                key = (institution, dt)
                if key in seen:
                    continue
                seen.add(key)
                doc_id = conn.execute(
                    "SELECT id FROM tax_documents WHERE year=? AND institution=? AND doc_type=?",
                    (year, institution, dt),
                ).fetchone()
                if doc_id:
                    skipped += 1
                else:
                    upsert_tax_document(conn, year, institution, dt)
                    inserted += 1
                    console.print(f"  [green]+[/] {institution}: {dt}")

        conn.commit()
        console.print(
            f"\n[bold]Done.[/] Added {inserted} document(s). "
            f"{skipped} already existed.\n"
            f"  [dim]Don't forget to add your employer W-2 manually:[/]\n"
            f"  fintrack tax docs add --year {year} --institution \"Employer\" --doc-type W-2"
        )
    finally:
        conn.close()


@tax_docs_app.command("rm")
def tax_docs_rm(
    doc_id: int = typer.Argument(..., help="Document ID from 'fintrack tax docs list'"),
):
    """Remove a tracked document entry."""
    from .db import delete_tax_document

    conn = _open_db()
    try:
        ok = delete_tax_document(conn, doc_id)
        conn.commit()
        if ok:
            console.print(f"[green]Document #{doc_id} removed.[/]")
        else:
            console.print(f"[yellow]No document with ID {doc_id}.[/]")
    finally:
        conn.close()


# Ordered most-specific first so e.g. "1098-E" matches before "1098".
_DOC_TYPE_PATTERNS: list[tuple[str, str]] = [
    ("w-2",      "W-2"),
    ("w2",       "W-2"),
    ("1099-int",  "1099-INT"),
    ("1099int",   "1099-INT"),
    ("1099-div",  "1099-DIV"),
    ("1099div",   "1099-DIV"),
    ("1099-nec",  "1099-NEC"),
    ("1099nec",   "1099-NEC"),
    ("1099-misc", "1099-MISC"),
    ("1099misc",  "1099-MISC"),
    ("1099-b",    "1099-B"),
    ("1099b",     "1099-B"),
    ("1099-r",    "1099-R"),
    ("1099r",     "1099-R"),
    ("1099-sa",   "1099-SA"),
    ("1099sa",    "1099-SA"),
    ("ssa-1099",  "SSA-1099"),
    ("ssa1099",   "SSA-1099"),
    ("1098-e",    "1098-E"),
    ("1098e",     "1098-E"),
    ("1098-t",    "1098-T"),
    ("1098t",     "1098-T"),
    ("1098",      "1098"),
    ("1099",      "other"),   # generic unspecified 1099
]


def _match_doc_type(filename: str) -> str | None:
    """Infer a tax document type from a filename. Returns None if unrecognized."""
    fname = filename.lower()
    for pattern, doc_type in _DOC_TYPE_PATTERNS:
        if pattern in fname:
            return doc_type
    return None


def _institution_tokens(institution: str) -> list[str]:
    """Lower-case alpha-numeric tokens from an institution name (length >= 3)."""
    import re
    return [t for t in re.findall(r'[a-z0-9]+', institution.lower()) if len(t) >= 3]


@tax_docs_app.command("scan")
def tax_docs_scan(
    folder: str = typer.Argument(..., help="Folder to scan (e.g. tax_documents or tax_documents/2025)"),
    year: Optional[int] = typer.Option(
        None, "--year", "-y",
        help="Tax year to match against expected documents (default: current year).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-n",
        help="Show matches without updating file_path in the database.",
    ),
):
    """
    Scan a local folder for tax document files and match them to expected entries.

    Looks for common document-type keywords (W-2, 1099-INT, 1098, etc.) in
    filenames and links matched files to their tracked entries so
    'fintrack tax docs list' can show which expected documents have a file on disk.

    Recommended folder layout (keep outside repo or add to .gitignore):

      tax_documents/
        2024/
          W2_Employer_2024.pdf
          1099-B_Schwab_2024.pdf
        2025/
          1099-INT_BofA_2025.pdf

    Usage:
      fintrack tax docs scan ./tax_documents --year 2025
      fintrack tax docs scan ./tax_documents/2025          # year auto-detected from path
    """
    import os
    from pathlib import Path
    from collections import defaultdict
    from .db import get_tax_documents, set_tax_document_file_path

    if year is None:
        # Try to detect year from last path component
        last = Path(folder).name
        if last.isdigit() and 2000 <= int(last) <= 2100:
            year = int(last)
        else:
            year = date.today().year

    # Resolve scan path: if folder/<year>/ exists, prefer it; else use folder directly.
    base = Path(folder)
    year_sub = base / str(year)
    scan_path = year_sub if year_sub.is_dir() else base

    if not scan_path.exists():
        console.print(f"[red]Folder not found: {scan_path}[/]")
        raise typer.Exit(1)

    files = sorted(f for f in scan_path.iterdir() if f.is_file())
    if not files:
        console.print(f"[yellow]No files found in {scan_path}[/]")
        return

    conn = _open_db()
    try:
        expected = get_tax_documents(conn, year=year)
        if not expected:
            console.print(
                f"[yellow]No expected documents tracked for {year}. "
                f"Run [bold]fintrack tax docs init --year {year}[/] first.[/]"
            )
            return

        # Group expected docs by doc_type for matching.
        by_type: dict[str, list[dict]] = defaultdict(list)
        for doc in expected:
            by_type[doc["doc_type"]].append(doc)

        matched_doc_ids: set[int] = set()
        matches: list[tuple] = []   # (file, inferred_type, doc)
        unmatched_files: list[str] = []

        for f in files:
            inferred = _match_doc_type(f.name)
            if inferred is None:
                unmatched_files.append(f.name)
                continue

            candidates = [
                d for d in by_type.get(inferred, [])
                if d["id"] not in matched_doc_ids
            ]
            if not candidates:
                unmatched_files.append(f.name)
                continue

            # Prefer candidate whose institution name has a token in the filename.
            if len(candidates) > 1:
                fname_lower = f.name.lower()
                inst_scored = [
                    (sum(1 for tok in _institution_tokens(c["institution"]) if tok in fname_lower), c)
                    for c in candidates
                ]
                best = max(inst_scored, key=lambda x: x[0])[1]
            else:
                best = candidates[0]

            matched_doc_ids.add(best["id"])
            matches.append((f.name, inferred, best, str(f)))

        # Print results
        console.print(f"\nScanned [bold]{scan_path}[/] ({len(files)} file(s)) — year {year}\n")

        if matches:
            mt = Table(title=f"Matched ({len(matches)})")
            mt.add_column("File")
            mt.add_column("Inferred Type")
            mt.add_column("Institution")
            mt.add_column("DB ID", justify="right", style="dim")
            mt.add_column("Updated" if not dry_run else "Would update", justify="center")
            for fname, inferred, doc, fpath in matches:
                updated = "[dim]--[/]" if dry_run else "[green]yes[/]"
                mt.add_row(fname[:45], inferred, doc["institution"], str(doc["id"]), updated)
            console.print(mt)

        unmatched_expected = [d for d in expected if d["id"] not in matched_doc_ids]
        if unmatched_expected:
            ut = Table(title=f"Expected but no file found ({len(unmatched_expected)})")
            ut.add_column("ID", justify="right", style="dim")
            ut.add_column("Institution")
            ut.add_column("Doc Type")
            ut.add_column("Received", justify="center")
            for d in unmatched_expected:
                recv = "[green]yes[/]" if d["received"] else "[yellow]waiting[/]"
                ut.add_row(str(d["id"]), d["institution"], d["doc_type"], recv)
            console.print(ut)

        if unmatched_files:
            console.print(
                f"\n  [dim]{len(unmatched_files)} file(s) not matched to any expected document: "
                + ", ".join(unmatched_files[:5])
                + ("…" if len(unmatched_files) > 5 else "") + "[/]"
            )

        if not dry_run and matches:
            for _, _, doc, fpath in matches:
                set_tax_document_file_path(conn, doc["id"], fpath)
            conn.commit()
            console.print(
                f"\n[green]Updated file_path for {len(matches)} document(s).[/] "
                f"Run [bold]fintrack tax docs list --year {year}[/] to review."
            )
        elif dry_run and matches:
            console.print("\n[dim]Dry run — no changes written.[/]")

    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# fintrack tax info
# ══════════════════════════════════════════════════════════════════════════════

@tax_info_app.command("set")
def tax_info_set(
    key:   str = typer.Argument(..., help="Key name (e.g. employer_ein, prior_year_agi)"),
    value: str = typer.Argument(..., help="Value to store"),
    notes: Optional[str] = typer.Option(None, "--notes", "-n", help="Optional reminder note"),
):
    """
    Store a piece of static tax reference info as a key-value pair.

    Examples:
      fintrack tax info set employer_ein 12-3456789
      fintrack tax info set prior_year_agi 95000
      fintrack tax info set schwab_acct_last4 4321
      fintrack tax info set filing_status "head of household (verify with CPA)"

    WARNING: do NOT store full SSNs, full account numbers, or other
    complete sensitive identifiers. Last 4 digits are fine for reference.
    """
    from .db import set_tax_info

    conn = _open_db()
    try:
        set_tax_info(conn, key, value, notes)
        conn.commit()
        console.print(f"[green]Set[/] [bold]{key}[/] = {value}" + (f"  [dim]({notes})[/]" if notes else ""))
    finally:
        conn.close()


@tax_info_app.command("list")
def tax_info_list():
    """Show all stored tax reference info."""
    from .db import get_all_tax_info

    conn = _open_db()
    try:
        rows = get_all_tax_info(conn)
        if not rows:
            console.print(
                "[dim]No tax info stored. Use [bold]fintrack tax info set <key> <value>[/] to add.[/]"
            )
            return
        t = Table(title="Tax Reference Info", box=None)
        t.add_column("Key",      justify="left")
        t.add_column("Value",    justify="left")
        t.add_column("Notes",    justify="left", style="dim")
        t.add_column("Updated",  justify="left", style="dim")
        for r in rows:
            t.add_row(r["key"], r["value"], r["notes"] or "", r["updated_at"][:10])
        console.print(t)
    finally:
        conn.close()


@tax_info_app.command("rm")
def tax_info_rm(
    key: str = typer.Argument(..., help="Key to remove"),
):
    """Remove a tax info entry."""
    from .db import delete_tax_info

    conn = _open_db()
    try:
        ok = delete_tax_info(conn, key)
        conn.commit()
        if ok:
            console.print(f"[green]Removed tax info key '{key}'.[/]")
        else:
            console.print(f"[yellow]No entry with key '{key}'.[/]")
    finally:
        conn.close()
