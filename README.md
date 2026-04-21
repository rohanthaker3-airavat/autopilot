# Inventory + Owe Tracker

A lightweight Streamlit app to:

- Track inventory (stock, average cost, expected selling price).
- Track purchase history.
- Upload bill screenshots and attempt OCR extraction.
- Record who paid and who the purchase is for.
- See a dashboard of who owes you vs who you owe.

## Features

1. **Inventory tracking**
   - Automatic quantity updates by product name.
   - Weighted average cost calculation.

2. **Money tracking / owing ledger**
   - If `payer = Me` and `beneficiary = Someone Else`, they owe you.
   - If `payer = Someone Else` and `beneficiary = Me`, you owe them.
   - Dashboard shows both directions and net balance.

3. **Bill screenshot extraction**
   - Upload `.png/.jpg/.jpeg` bills.
   - Optional OCR extraction using `pytesseract`.
   - Parsed merchant/date/total + possible line items are prefilled for manual confirmation.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> For OCR support, you also need the **system Tesseract binary** installed.

## Run

```bash
streamlit run app.py
```

The app stores data in local SQLite file `inventory_tracker.db` and bill images in `./bills/`.
