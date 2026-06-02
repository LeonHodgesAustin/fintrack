"""
Google Sheets integration for fintrack.

Tabs:
  Summary      -- Current-month category breakdown + top merchants
  Trends       -- Category x month cross-section matrix (white->orange gradient)
  Cashflow     -- Monthly income / expense / net trend
  Transactions -- Recent transactions with Override Category column for corrections
  Forecast     -- Prophet spending forecasts (optional, requires prophet)

Override roundtrip:
  push() first reads any filled "Override Category" cells from the Transactions
  tab and commits them to the local transaction_overrides table before rewriting
  the sheet. This means you can correct categories directly in Sheets and they
  will survive the next sync.

Auth: Google service account (same pattern as Ethan_homework_helper).
Required .env vars:
  GOOGLE_SERVICE_ACCOUNT_FILE  -- path to your service_account.json
  GOOGLE_SPREADSHEET_ID        -- the ID from the sheet URL
"""

import sqlite3
from datetime import date, datetime, timezone
from typing import Any

import gspread
from google.oauth2.service_account import Credentials

from .cashflow import cashflow_summary, cashflow_trend
from .reports import category_trends, monthly_summary, recent_transactions, top_merchants

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

_HEADER_BG   = {"red": 0.204, "green": 0.267, "blue": 0.341}
_SECTION_BG  = {"red": 0.851, "green": 0.886, "blue": 0.953}
_FOOTER_BG   = {"red": 0.918, "green": 0.918, "blue": 0.918}
_WHITE       = {"red": 1.0,   "green": 1.0,   "blue": 1.0  }
_HEADER_TEXT = {"red": 1.0,   "green": 1.0,   "blue": 1.0  }
_OVERRIDE_BG = {"red": 1.0,   "green": 1.0,   "blue": 0.8  }  # light yellow for override column
_GRADIENT_MIN = {"red": 1.0,   "green": 1.0,   "blue": 1.0  }
_GRADIENT_MAX = {"red": 0.957, "green": 0.643, "blue": 0.376}
_PENDING_BG   = {"red": 1.0,   "green": 0.976, "blue": 0.769}
_INCOME_BG    = {"red": 0.784, "green": 0.902, "blue": 0.788}
_EXPENSE_BG   = {"red": 1.0,   "green": 0.878, "blue": 0.878}

# Column indices in the Transactions tab (0-based)
_TXN_COLS = ["Date", "Institution", "Account", "Merchant",
             "Category", "Subcategory", "Amount ($)", "Classifier", "Pending",
             "Override Category", "Override Note"]
_COL_OVERRIDE_CAT  = 9
_COL_OVERRIDE_NOTE = 10
_COL_TXN_ID        = None  # not shown; we track by row position via merchant+date


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _month_label(year: int, month: int) -> str:
    return date(year, month, 1).strftime("%b %Y")


class FintrackSheetsClient:
    def __init__(self, service_account_file: str, spreadsheet_id: str):
        creds = Credentials.from_service_account_file(service_account_file, scopes=_SCOPES)
        gc = gspread.authorize(creds)
        self._spreadsheet = gc.open_by_key(spreadsheet_id)
        self._spreadsheet_id = spreadsheet_id

    # -- Helpers ---------------------------------------------------------------

    def _get_or_create_tab(self, name: str, rows: int = 500, cols: int = 30) -> gspread.Worksheet:
        try:
            return self._spreadsheet.worksheet(name)
        except gspread.exceptions.WorksheetNotFound:
            return self._spreadsheet.add_worksheet(title=name, rows=rows, cols=cols)

    def _freeze(self, ws: gspread.Worksheet, rows: int = 1, cols: int = 0) -> None:
        self._spreadsheet.batch_update({"requests": [{
            "updateSheetProperties": {
                "properties": {
                    "sheetId": ws.id,
                    "gridProperties": {"frozenRowCount": rows, "frozenColumnCount": cols},
                },
                "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
            }
        }]})

    def _clear_conditional_formats(self, ws: gspread.Worksheet) -> None:
        for _ in range(30):
            try:
                self._spreadsheet.batch_update({"requests": [{
                    "deleteConditionalFormatRule": {"sheetId": ws.id, "index": 0}
                }]})
            except Exception:
                break

    def _format_row(self, ws: gspread.Worksheet, row_index: int, n_cols: int,
                    bg: dict, bold: bool = False, font_size: int | None = None) -> None:
        fmt: dict = {"backgroundColor": bg, "textFormat": {"bold": bold}}
        if font_size:
            fmt["textFormat"]["fontSize"] = font_size
        self._spreadsheet.batch_update({"requests": [{
            "repeatCell": {
                "range": {"sheetId": ws.id, "startRowIndex": row_index,
                           "endRowIndex": row_index + 1,
                           "startColumnIndex": 0, "endColumnIndex": n_cols},
                "cell": {"userEnteredFormat": fmt},
                "fields": "userEnteredFormat(backgroundColor,textFormat)",
            }
        }]})

    def _format_col_bg(self, ws: gspread.Worksheet, col_index: int,
                       start_row: int, n_rows: int, bg: dict) -> None:
        self._spreadsheet.batch_update({"requests": [{
            "repeatCell": {
                "range": {"sheetId": ws.id, "startRowIndex": start_row,
                           "endRowIndex": start_row + n_rows,
                           "startColumnIndex": col_index, "endColumnIndex": col_index + 1},
                "cell": {"userEnteredFormat": {"backgroundColor": bg}},
                "fields": "userEnteredFormat(backgroundColor)",
            }
        }]})

    def _apply_gradient(self, ws: gspread.Worksheet,
                        start_row: int, start_col: int,
                        n_rows: int, n_cols: int) -> None:
        self._spreadsheet.batch_update({"requests": [{
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{"sheetId": ws.id,
                                "startRowIndex": start_row, "endRowIndex": start_row + n_rows,
                                "startColumnIndex": start_col, "endColumnIndex": start_col + n_cols}],
                    "gradientRule": {
                        "minpoint": {"colorStyle": {"rgbColor": _GRADIENT_MIN},
                                     "type": "NUMBER", "value": "0"},
                        "maxpoint": {"colorStyle": {"rgbColor": _GRADIENT_MAX}, "type": "MAX"},
                    },
                },
                "index": 0,
            }
        }]})

    def _set_col_width(self, ws: gspread.Worksheet, col_index: int, px: int) -> None:
        self._spreadsheet.batch_update({"requests": [{
            "updateDimensionProperties": {
                "range": {"sheetId": ws.id, "dimension": "COLUMNS",
                           "startIndex": col_index, "endIndex": col_index + 1},
                "properties": {"pixelSize": px},
                "fields": "pixelSize",
            }
        }]})

    # -- Override roundtrip ----------------------------------------------------

    def pull_overrides(self, conn: sqlite3.Connection) -> int:
        """
        Read Override Category / Override Note columns from the Transactions tab
        and write any filled values to the transaction_overrides DB table.

        Matches rows by transaction_id stored in a hidden column (col index 11,
        written during push_transactions but not shown to the user).

        Returns the count of overrides applied.
        """
        from .db import set_override
        try:
            ws = self._spreadsheet.worksheet("Transactions")
        except gspread.exceptions.WorksheetNotFound:
            return 0

        all_rows = ws.get_all_values()
        if len(all_rows) < 2:
            return 0

        header = all_rows[0]
        # Find column indices
        try:
            cat_col  = header.index("Override Category")
            note_col = header.index("Override Note")
            id_col   = header.index("_txn_id")  # hidden column
        except ValueError:
            return 0

        count = 0
        for row in all_rows[1:]:
            if len(row) <= max(cat_col, note_col, id_col):
                continue
            txn_id   = row[id_col].strip()
            override = row[cat_col].strip().upper()
            note     = row[note_col].strip()
            if txn_id and override:
                set_override(conn, txn_id, override, note=note or None, source="sheets")
                count += 1

        if count:
            conn.commit()
        return count

    # -- Summary tab -----------------------------------------------------------

    def push_summary(self, conn: sqlite3.Connection, year: int, month: int) -> None:
        ws = self._get_or_create_tab("Summary", rows=200, cols=5)
        ws.clear()
        self._clear_conditional_formats(ws)

        label   = date(year, month, 1).strftime("%B %Y")
        summary = monthly_summary(conn, year, month)
        merchants = top_merchants(conn, year, month, limit=15)
        total_spend = sum(r["total_amount"] for r in summary)
        total_txns  = sum(r["transaction_count"] for r in summary)

        rows: list[list[Any]] = [
            [f"FinTrack -- {label}", "", "", "", ""],
            [f"Last updated: {_now_str()}", "", "", "", ""],
            [""],
            ["SPENDING BY CATEGORY", "", "", "", ""],
            ["Category", "Amount ($)", "Txns", "% of Total", ""],
        ]
        cat_section_row = 3
        for r in summary:
            pct = f"{r['total_amount'] / total_spend * 100:.1f}%" if total_spend else "0%"
            rows.append([r["category"], round(r["total_amount"], 2),
                         r["transaction_count"], pct, ""])
        total_row_idx = len(rows)
        rows.append(["TOTAL", round(total_spend, 2), total_txns, "100%", ""])
        rows.append([""])
        merch_section_row = len(rows)
        rows.append(["TOP MERCHANTS", "", "", "", ""])
        rows.append(["#", "Merchant", "Amount ($)", "Txns", ""])
        for i, r in enumerate(merchants, 1):
            rows.append([i, r["merchant"], round(r["total_amount"], 2),
                         r["transaction_count"], ""])

        ws.update("A1", rows)

        requests = []
        # Title row
        requests.append({"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1,
                       "startColumnIndex": 0, "endColumnIndex": 5},
            "cell": {"userEnteredFormat": {
                "backgroundColor": _HEADER_BG,
                "textFormat": {"bold": True, "foregroundColor": _HEADER_TEXT, "fontSize": 13},
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat)",
        }})
        # Section rows
        for idx in [cat_section_row, merch_section_row]:
            requests.append({"repeatCell": {
                "range": {"sheetId": ws.id, "startRowIndex": idx, "endRowIndex": idx + 1,
                           "startColumnIndex": 0, "endColumnIndex": 5},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": _SECTION_BG, "textFormat": {"bold": True},
                }},
                "fields": "userEnteredFormat(backgroundColor,textFormat)",
            }})
        # TOTAL footer
        requests.append({"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": total_row_idx,
                       "endRowIndex": total_row_idx + 1,
                       "startColumnIndex": 0, "endColumnIndex": 5},
            "cell": {"userEnteredFormat": {
                "backgroundColor": _FOOTER_BG, "textFormat": {"bold": True},
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat)",
        }})
        self._spreadsheet.batch_update({"requests": requests})
        self._freeze(ws, rows=1)
        self._set_col_width(ws, 0, 240)

    # -- Trends tab ------------------------------------------------------------

    def push_trends(self, conn: sqlite3.Connection, months: int = 12) -> None:
        ws = self._get_or_create_tab("Trends", rows=100, cols=50)
        ws.clear()
        self._clear_conditional_formats(ws)

        trends = category_trends(conn, months=months)
        if not trends:
            ws.update("A1", [["No transaction data yet -- run fintrack sync first."]])
            return

        month_set: set[tuple] = set()
        for series in trends.values():
            for pt in series:
                month_set.add((pt["year"], pt["month"]))
        month_cols = sorted(month_set)

        cat_totals = {cat: sum(pt["total_amount"] for pt in s) for cat, s in trends.items()}
        categories = sorted(cat_totals, key=lambda c: cat_totals[c], reverse=True)

        lookup: dict[tuple, float] = {}
        for cat, series in trends.items():
            for pt in series:
                lookup[(cat, pt["year"], pt["month"])] = pt["total_amount"]

        header: list[Any] = (["Category"]
                              + [_month_label(y, m) for y, m in month_cols]
                              + ["Total"])
        data_rows: list[list[Any]] = [header]
        for cat in categories:
            amounts = [lookup.get((cat, y, m), 0.0) for y, m in month_cols]
            data_rows.append([cat] + [round(a, 2) for a in amounts] + [round(sum(amounts), 2)])

        col_sums = [sum(lookup.get((cat, y, m), 0.0) for cat in categories) for y, m in month_cols]
        data_rows.append(["TOTAL"] + [round(s, 2) for s in col_sums] + [round(sum(col_sums), 2)])

        ws.update("A1", data_rows)

        n_cats   = len(categories)
        n_months = len(month_cols)
        n_cols   = len(header)

        self._format_row(ws, 0, n_cols, _HEADER_BG, bold=True)
        self._freeze(ws, rows=1, cols=1)
        self._set_col_width(ws, 0, 280)
        footer_row = 1 + n_cats
        self._format_row(ws, footer_row, n_cols, _FOOTER_BG, bold=True)

        if n_cats > 0 and n_months > 0:
            self._apply_gradient(ws, start_row=1, start_col=1,
                                 n_rows=n_cats, n_cols=n_months)

    # -- Cashflow tab ----------------------------------------------------------

    def push_cashflow(
        self,
        conn: sqlite3.Connection,
        year: int,
        month: int,
        months_trend: int = 12,
        transfer_categories: frozenset | None = None,
    ) -> None:
        ws = self._get_or_create_tab("Cashflow", rows=100, cols=10)
        ws.clear()
        self._clear_conditional_formats(ws)

        cf = cashflow_summary(conn, year, month, transfer_categories=transfer_categories)
        trend = cashflow_trend(conn, months=months_trend, transfer_categories=transfer_categories)
        label = date(year, month, 1).strftime("%B %Y")

        rows: list[list[Any]] = [
            [f"FinTrack Cashflow -- {label}", "", "", ""],
            [f"Last updated: {_now_str()}", "", "", ""],
            [""],
            ["CURRENT MONTH", "", "", ""],
            ["Income",        round(cf["income"],        2), "", ""],
            ["Expenses",      round(cf["expenses"],      2), "", ""],
            ["Net",           round(cf["net"],           2), "", ""],
            ["Transfers In",  round(cf["transfers_in"],  2), "", ""],
            ["Transfers Out", round(cf["transfers_out"], 2), "", ""],
            ["Internal Transfer Pairs", cf["internal_pairs"], "", ""],
            [""],
            ["MONTHLY TREND", "", "", ""],
            ["Month", "Income", "Expenses", "Net"],
        ]
        for row in trend:
            rows.append([
                f"{row['year']}-{row['month']:02d}",
                round(row["income"], 2),
                round(row["expenses"], 2),
                round(row["net"], 2),
            ])

        ws.update("A1", rows)

        requests = []
        requests.append({"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1,
                       "startColumnIndex": 0, "endColumnIndex": 4},
            "cell": {"userEnteredFormat": {
                "backgroundColor": _HEADER_BG,
                "textFormat": {"bold": True, "foregroundColor": _HEADER_TEXT, "fontSize": 13},
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat)",
        }})
        for section_row in [3, 11]:
            requests.append({"repeatCell": {
                "range": {"sheetId": ws.id, "startRowIndex": section_row,
                           "endRowIndex": section_row + 1,
                           "startColumnIndex": 0, "endColumnIndex": 4},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": _SECTION_BG, "textFormat": {"bold": True},
                }},
                "fields": "userEnteredFormat(backgroundColor,textFormat)",
            }})
        # Income row (row 4) green, Expenses (row 5) red, Net (row 6) depends
        requests.append({"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": 4, "endRowIndex": 5,
                       "startColumnIndex": 0, "endColumnIndex": 4},
            "cell": {"userEnteredFormat": {"backgroundColor": _INCOME_BG}},
            "fields": "userEnteredFormat(backgroundColor)",
        }})
        requests.append({"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": 5, "endRowIndex": 6,
                       "startColumnIndex": 0, "endColumnIndex": 4},
            "cell": {"userEnteredFormat": {"backgroundColor": _EXPENSE_BG}},
            "fields": "userEnteredFormat(backgroundColor)",
        }})
        self._spreadsheet.batch_update({"requests": requests})
        self._freeze(ws, rows=1)

    # -- Transactions tab (with override columns) ------------------------------

    def push_transactions(self, conn: sqlite3.Connection, days: int = 90) -> None:
        ws = self._get_or_create_tab("Transactions", rows=5000, cols=12)
        ws.clear()
        self._clear_conditional_formats(ws)

        txns = recent_transactions(conn, days=days)

        # _txn_id is a hidden tracking column -- pull_overrides uses it to match rows
        header = _TXN_COLS + ["_txn_id"]
        rows: list[list[Any]] = [header]
        for t in txns:
            rows.append([
                t["date"],
                t.get("institution_name", ""),
                t.get("account_name", ""),
                t.get("merchant_name") or t.get("raw_name") or "",
                t.get("category_primary") or "",
                t.get("category_detailed") or "",
                round(t["amount"], 2),
                t.get("category_source") or "",
                "pending" if t.get("pending") else "",
                "",   # Override Category -- user fills this in
                "",   # Override Note     -- user fills this in
                t["transaction_id"],  # _txn_id (hidden)
            ])

        ws.update("A1", rows)

        n_cols = len(header)
        requests = []
        # Header row
        requests.append({"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1,
                       "startColumnIndex": 0, "endColumnIndex": n_cols},
            "cell": {"userEnteredFormat": {
                "backgroundColor": _HEADER_BG,
                "textFormat": {"bold": True, "foregroundColor": _HEADER_TEXT},
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat)",
        }})
        # Highlight override columns
        n_data = len(txns)
        if n_data > 0:
            for col_i in [_COL_OVERRIDE_CAT, _COL_OVERRIDE_NOTE]:
                requests.append({"repeatCell": {
                    "range": {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": 1 + n_data,
                               "startColumnIndex": col_i, "endColumnIndex": col_i + 1},
                    "cell": {"userEnteredFormat": {"backgroundColor": _OVERRIDE_BG}},
                    "fields": "userEnteredFormat(backgroundColor)",
                }})
        # Pending highlight via conditional format
        if n_data > 0:
            self._spreadsheet.batch_update({"requests": requests})
            requests = []
            self._spreadsheet.batch_update({"requests": [{
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [{"sheetId": ws.id, "startRowIndex": 1,
                                    "startColumnIndex": 0, "endColumnIndex": n_cols}],
                        "booleanRule": {
                            "condition": {"type": "TEXT_EQ",
                                          "values": [{"userEnteredValue": "pending"}]},
                            "format": {"backgroundColor": _PENDING_BG},
                        },
                    },
                    "index": 0,
                }
            }]})

        if requests:
            self._spreadsheet.batch_update({"requests": requests})

        self._freeze(ws, rows=1)
        # Hide the _txn_id column
        self._spreadsheet.batch_update({"requests": [{
            "updateDimensionProperties": {
                "range": {"sheetId": ws.id, "dimension": "COLUMNS",
                           "startIndex": 11, "endIndex": 12},
                "properties": {"hiddenByUser": True, "pixelSize": 0},
                "fields": "hiddenByUser,pixelSize",
            }
        }]})
        for col, px in [(0, 100), (1, 140), (2, 160), (3, 220),
                        (4, 180), (5, 240), (9, 180), (10, 200)]:
            self._set_col_width(ws, col, px)

    # -- Forecast tab ----------------------------------------------------------

    def push_forecast(self, conn: sqlite3.Connection, months_ahead: int = 3) -> None:
        """Requires prophet. Raises ImportError if not installed."""
        from .forecasting import forecast_all_categories, detect_anomalous_months

        ws = self._get_or_create_tab("Forecast", rows=200, cols=8)
        ws.clear()
        self._clear_conditional_formats(ws)

        forecasts = forecast_all_categories(conn, months_ahead=months_ahead)
        if not forecasts:
            ws.update("A1", [["Not enough history for forecasts yet."]])
            return

        header = ["Category", "Month", "Forecast ($)", "Low ($)", "High ($)", "Type"]
        rows: list[list[Any]] = [
            [f"FinTrack Spending Forecast -- next {months_ahead} month(s)", "", "", "", "", ""],
            [f"Last updated: {_now_str()}", "", "", "", "", ""],
            [""],
            header,
        ]
        for cat, fc_rows in forecasts.items():
            for r in fc_rows:
                row_type = "FORECAST" if r["is_forecast"] else "historical"
                rows.append([
                    cat if r["is_forecast"] else "",
                    r["ds"][:7],
                    round(r["yhat"], 2),
                    round(r["yhat_lower"], 2),
                    round(r["yhat_upper"], 2),
                    row_type,
                ])

        # Anomalies section
        anomalies = detect_anomalous_months(conn)
        if anomalies:
            rows.append([""])
            rows.append(["ANOMALOUS MONTHS (actual >> expected)", "", "", "", "", ""])
            rows.append(["Category", "Month", "Actual ($)", "Expected ($)", "Sigma", "Method"])
            for a in anomalies:
                rows.append([a["category"], a["month_ds"], a["actual"],
                              a["expected"], a["sigma"], a["method"]])

        ws.update("A1", rows)
        self._format_row(ws, 0, 6, _HEADER_BG, bold=True, font_size=12)
        self._format_row(ws, 3, 6, _SECTION_BG, bold=True)
        self._freeze(ws, rows=4)
        self._set_col_width(ws, 0, 220)

    # -- Push all --------------------------------------------------------------

    def push_all(
        self,
        conn: sqlite3.Connection,
        year: int,
        month: int,
        trend_months: int = 12,
        txn_days: int = 90,
        transfer_categories: frozenset | None = None,
        include_forecast: bool = False,
    ) -> None:
        n_overrides = self.pull_overrides(conn)
        self.push_summary(conn, year, month)
        self.push_trends(conn, months=trend_months)
        self.push_cashflow(conn, year, month, months_trend=trend_months,
                           transfer_categories=transfer_categories)
        self.push_transactions(conn, days=txn_days)
        if include_forecast:
            try:
                self.push_forecast(conn)
            except ImportError:
                pass
