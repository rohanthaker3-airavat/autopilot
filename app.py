import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st
from PIL import Image

DB_PATH = Path("inventory_tracker.db")
BILL_DIR = Path("bills")
BILL_DIR.mkdir(exist_ok=True)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            sku TEXT,
            quantity REAL NOT NULL DEFAULT 0,
            avg_cost REAL NOT NULL DEFAULT 0,
            selling_price REAL NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS counterparties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            product_name TEXT NOT NULL,
            quantity REAL NOT NULL,
            unit_cost REAL NOT NULL,
            total_amount REAL NOT NULL,
            payer TEXT NOT NULL,
            beneficiary TEXT NOT NULL,
            source TEXT,
            bill_image_path TEXT,
            raw_ocr_text TEXT,
            note TEXT
        )
        """
    )
    conn.commit()
    conn.close()


init_db()


def ensure_counterparty(name: str) -> None:
    if not name:
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO counterparties(name) VALUES(?)", (name.strip(),))
    conn.commit()
    conn.close()


def get_counterparties() -> List[str]:
    conn = get_conn()
    df = pd.read_sql_query("SELECT name FROM counterparties ORDER BY name", conn)
    conn.close()
    names = df["name"].tolist()
    if "Me" not in names:
        names = ["Me"] + names
    return names


def upsert_product(name: str, quantity: float, unit_cost: float, selling_price: float) -> None:
    conn = get_conn()
    cur = conn.cursor()
    row = cur.execute("SELECT id, quantity, avg_cost FROM products WHERE lower(name)=lower(?)", (name,)).fetchone()
    now = datetime.utcnow().isoformat()

    if row:
        old_qty = float(row["quantity"])
        old_avg_cost = float(row["avg_cost"])
        new_qty = old_qty + quantity
        if new_qty <= 0:
            weighted_cost = old_avg_cost
        else:
            weighted_cost = ((old_qty * old_avg_cost) + (quantity * unit_cost)) / new_qty
        cur.execute(
            """
            UPDATE products
            SET quantity=?, avg_cost=?, selling_price=?, updated_at=?
            WHERE id=?
            """,
            (new_qty, weighted_cost, selling_price, now, row["id"]),
        )
    else:
        cur.execute(
            """
            INSERT INTO products(name, quantity, avg_cost, selling_price, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (name, quantity, unit_cost, selling_price, now),
        )

    conn.commit()
    conn.close()


def add_purchase(
    product_name: str,
    quantity: float,
    unit_cost: float,
    payer: str,
    beneficiary: str,
    source: str,
    bill_image_path: Optional[str],
    raw_ocr_text: Optional[str],
    note: str,
) -> None:
    total = quantity * unit_cost
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO purchases(
            created_at, product_name, quantity, unit_cost, total_amount,
            payer, beneficiary, source, bill_image_path, raw_ocr_text, note
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.utcnow().isoformat(),
            product_name,
            quantity,
            unit_cost,
            total,
            payer,
            beneficiary,
            source,
            bill_image_path,
            raw_ocr_text,
            note,
        ),
    )
    conn.commit()
    conn.close()


@dataclass
class BillParseResult:
    merchant: Optional[str]
    date: Optional[str]
    total: Optional[float]
    possible_items: List[Tuple[str, float]]
    raw_text: str


def run_ocr(image: Image.Image) -> str:
    try:
        import pytesseract

        text = pytesseract.image_to_string(image)
        return text
    except Exception as exc:
        st.warning(
            "OCR engine unavailable. Install system Tesseract + pytesseract to auto-read bills. "
            f"Error: {exc}"
        )
        return ""


def parse_bill_text(text: str) -> BillParseResult:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    merchant = lines[0] if lines else None

    date_match = re.search(r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", text)
    date = date_match.group(1) if date_match else None

    totals = []
    for m in re.finditer(r"(?:total|amount|grand\s*total)[^\d]*(\d+[\.,]\d{2})", text, re.IGNORECASE):
        candidate = m.group(1).replace(",", ".")
        try:
            totals.append(float(candidate))
        except ValueError:
            pass
    total = max(totals) if totals else None

    item_candidates: List[Tuple[str, float]] = []
    for line in lines:
        m = re.search(r"([A-Za-z][A-Za-z0-9\-\s]{2,})\s+(\d+[\.,]\d{2})$", line)
        if m:
            name = re.sub(r"\s+", " ", m.group(1)).strip()
            amount = float(m.group(2).replace(",", "."))
            item_candidates.append((name, amount))

    return BillParseResult(merchant=merchant, date=date, total=total, possible_items=item_candidates[:8], raw_text=text)


def compute_balances() -> Dict[str, Dict[str, float]]:
    conn = get_conn()
    df = pd.read_sql_query("SELECT payer, beneficiary, total_amount FROM purchases", conn)
    conn.close()
    balances: Dict[str, Dict[str, float]] = {}

    for _, row in df.iterrows():
        payer = row["payer"]
        beneficiary = row["beneficiary"]
        amount = float(row["total_amount"])

        if payer == beneficiary:
            continue

        if payer == "Me" and beneficiary != "Me":
            entry = balances.setdefault(beneficiary, {"they_owe_me": 0.0, "i_owe_them": 0.0})
            entry["they_owe_me"] += amount
        elif payer != "Me" and beneficiary == "Me":
            entry = balances.setdefault(payer, {"they_owe_me": 0.0, "i_owe_them": 0.0})
            entry["i_owe_them"] += amount

    return balances


def inventory_value(df_products: pd.DataFrame) -> float:
    if df_products.empty:
        return 0.0
    return float((df_products["quantity"] * df_products["avg_cost"]).sum())


st.set_page_config(page_title="Inventory & Owe Tracker", layout="wide")
st.title("📦 Inventory + Money Owe Tracker")
st.caption(
    "Upload bill screenshots, extract details, and track purchases made for yourself or on behalf of others."
)

with st.sidebar:
    st.header("People")
    person = st.text_input("Add person/company")
    if st.button("Save person") and person.strip():
        ensure_counterparty(person)
        st.success(f"Saved {person}")

    st.divider()
    st.write("Parties in system:")
    for p in get_counterparties():
        st.write(f"• {p}")

balances = compute_balances()
conn = get_conn()
products_df = pd.read_sql_query("SELECT * FROM products ORDER BY updated_at DESC", conn)
purchases_df = pd.read_sql_query("SELECT * FROM purchases ORDER BY created_at DESC", conn)
conn.close()

col_a, col_b, col_c = st.columns(3)
with col_a:
    st.metric("Total SKUs", int(products_df.shape[0]))
with col_b:
    total_units = float(products_df["quantity"].sum()) if not products_df.empty else 0.0
    st.metric("Total Units in Stock", f"{total_units:.2f}")
with col_c:
    st.metric("Inventory Cost Value", f"${inventory_value(products_df):,.2f}")

st.subheader("💸 Owe Dashboard")
if not balances:
    st.info("No cross-party purchases yet. Set payer/beneficiary differently to track dues.")
else:
    bal_rows = []
    for name, b in balances.items():
        net = b["they_owe_me"] - b["i_owe_them"]
        bal_rows.append(
            {
                "Party": name,
                "They owe me": round(b["they_owe_me"], 2),
                "I owe them": round(b["i_owe_them"], 2),
                "Net (+ means they owe me)": round(net, 2),
            }
        )
    st.dataframe(pd.DataFrame(bal_rows), use_container_width=True)

st.divider()
add_tab, inventory_tab, history_tab = st.tabs(["Add Purchase", "Inventory", "Purchase History"])

with add_tab:
    st.markdown("### Add purchase manually or from bill screenshot")

    uploaded = st.file_uploader("Upload bill image", type=["png", "jpg", "jpeg"])

    parsed = None
    bill_path = None
    ocr_text = ""

    if uploaded:
        img = Image.open(uploaded)
        st.image(img, caption="Uploaded bill", use_column_width=True)

        stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        bill_path = BILL_DIR / f"bill_{stamp}_{uploaded.name}"
        img.save(bill_path)

        if st.button("Extract details from bill"):
            ocr_text = run_ocr(img)
            parsed = parse_bill_text(ocr_text)
            st.session_state["last_parse"] = parsed
            st.session_state["last_ocr_text"] = ocr_text
            st.session_state["last_bill_path"] = str(bill_path)

    if "last_parse" in st.session_state:
        parsed = st.session_state["last_parse"]
        st.write("#### Parsed bill details")
        st.json(
            {
                "merchant": parsed.merchant,
                "date": parsed.date,
                "total": parsed.total,
                "possible_items": parsed.possible_items,
            }
        )

    parties = get_counterparties()
    payer = st.selectbox("Who paid?", options=parties, index=0)
    beneficiary = st.selectbox("Who is this purchase for?", options=parties, index=0)

    default_product = ""
    default_qty = 1.0
    default_cost = 0.0
    if parsed and parsed.possible_items:
        default_product = parsed.possible_items[0][0]
        default_cost = parsed.possible_items[0][1]
    elif parsed and parsed.total:
        default_cost = parsed.total

    with st.form("purchase_form"):
        product_name = st.text_input("Product name", value=default_product)
        quantity = st.number_input("Quantity", min_value=0.0, value=default_qty, step=1.0)
        unit_cost = st.number_input("Unit cost", min_value=0.0, value=float(default_cost), step=0.01)
        selling_price = st.number_input("Expected selling price", min_value=0.0, value=0.0, step=0.01)
        source = st.text_input("Source / Vendor", value=parsed.merchant if parsed and parsed.merchant else "")
        note = st.text_area("Notes")

        submit = st.form_submit_button("Save purchase")
        if submit:
            if not product_name.strip() or quantity <= 0:
                st.error("Product name and quantity are required.")
            else:
                if payer != "Me":
                    ensure_counterparty(payer)
                if beneficiary != "Me":
                    ensure_counterparty(beneficiary)

                upsert_product(product_name.strip(), float(quantity), float(unit_cost), float(selling_price))

                raw_ocr = st.session_state.get("last_ocr_text", "")
                saved_bill = st.session_state.get("last_bill_path")

                add_purchase(
                    product_name=product_name.strip(),
                    quantity=float(quantity),
                    unit_cost=float(unit_cost),
                    payer=payer,
                    beneficiary=beneficiary,
                    source=source,
                    bill_image_path=saved_bill,
                    raw_ocr_text=raw_ocr,
                    note=note,
                )
                st.success("Purchase + inventory saved.")

with inventory_tab:
    st.markdown("### Current inventory")
    if products_df.empty:
        st.info("No products yet.")
    else:
        show_df = products_df[["name", "sku", "quantity", "avg_cost", "selling_price", "updated_at"]].copy()
        show_df.columns = ["Name", "SKU", "Quantity", "Avg Cost", "Selling Price", "Updated"]
        st.dataframe(show_df, use_container_width=True)

with history_tab:
    st.markdown("### Purchase history")
    if purchases_df.empty:
        st.info("No purchase records yet.")
    else:
        show_p = purchases_df[
            [
                "created_at",
                "product_name",
                "quantity",
                "unit_cost",
                "total_amount",
                "payer",
                "beneficiary",
                "source",
                "bill_image_path",
                "note",
            ]
        ].copy()
        show_p.columns = [
            "Created",
            "Product",
            "Qty",
            "Unit Cost",
            "Total",
            "Payer",
            "For",
            "Source",
            "Bill",
            "Note",
        ]
        st.dataframe(show_p, use_container_width=True)
