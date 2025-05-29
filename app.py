"""
Shopify Variant-Price Dashboard
• Flask 3  • Bootstrap 5  • Live toasts & loader
• Bulk GraphQL update with stagedUploadsCreate
"""

from __future__ import annotations

import json
import os
import queue
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.serving import is_running_from_reloader

# ─────────────── ENV & CONSTANTS ────────────────
load_dotenv()  # picks up .env locally or Render env

SHOP_DOMAIN = os.getenv("SHOP_DOMAIN")
API_TOKEN = os.getenv("API_TOKEN")
FLASK_SECRET = os.getenv("FLASK_SECRET", "change-me")
API_VERSION = "2024-04"
GRAPHQL_URL = f"https://{SHOP_DOMAIN}/admin/api/{API_VERSION}/graphql.json"

if not SHOP_DOMAIN or not API_TOKEN:
    raise RuntimeError("Set SHOP_DOMAIN and API_TOKEN in env!")

HEADERS_GQL = {
    "X-Shopify-Access-Token": API_TOKEN,
    "Content-Type": "application/json",
}

SURCHARGE_FILE = "variant_prices.json"

# ─────────────── FLASK APP ──────────────────────
app = Flask(__name__)
app.secret_key = FLASK_SECRET

# small in-memory queue to push log lines to browser
_log_q: "queue.SimpleQueue[str]" = queue.SimpleQueue()


def _log(msg: str) -> None:  # helper to push to queue
    _log_q.put_nowait(f"[{time.strftime('%H:%M:%S')}] {msg}")


# ─────────────── UTILITIES ──────────────────────
def load_prices() -> dict[str, Any]:
    with open(SURCHARGE_FILE, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_prices(data: dict[str, Any]) -> None:
    with open(SURCHARGE_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def gql(query: str, variables: dict | None = None) -> dict[str, Any]:
    r = requests.post(
        GRAPHQL_URL,
        headers=HEADERS_GQL,
        json={"query": query, "variables": variables or {}},
        timeout=60,
    )
    r.raise_for_status()
    j = r.json()
    if j.get("errors"):
        raise RuntimeError(j["errors"])
    return j["data"]


# ─────────── BULK-UPDATE HELPERS ────────────────
BULK_MUTATION = r"""
mutation bulk($stagedUploadPath: String!) {
  bulkOperationRunMutation(
    mutation: \"""" + r"""
mutation call($input: ProductVariantInput!) {
  productVariantUpdate(input: $input)
  { userErrors { field message } }
}
""" + r"""\""",
    stagedUploadPath: $stagedUploadPath
  ) { bulkOperation { id status } userErrors { field message } }
}
"""


def staged_upload(jsonl_path: Path) -> str:
    data = gql(
        """
    mutation($input:[StagedUploadInput!]!){
      stagedUploadsCreate(input:$input){
        stagedTargets { url resourceUrl parameters }
        userErrors     { field message }
      }
    }""",
        {
            "input": [
                {
                    "resource": "BULK_MUTATION_VARIABLES",
                    "filename": jsonl_path.name,
                    "mimeType": "text/jsonl",
                    "httpMethod": "PUT",
                }
            ]
        },
    )["stagedUploadsCreate"]
    if data["userErrors"]:
        raise RuntimeError(data["userErrors"])

    tgt = data["stagedTargets"][0]
    with open(jsonl_path, "rb") as fh:
        requests.put(
            tgt["url"],
            params={p["name"]: p["value"] for p in tgt["parameters"]},
            data=fh,
            headers={"Content-Type": "text/jsonl"},
            timeout=120,
        ).raise_for_status()
    return tgt["resourceUrl"]


def bulk_update(variant_map: dict[str, float]) -> None:
    _log(f"Preparing {len(variant_map)} variants…")
    with tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl") as tmp:
        for vid, price in variant_map.items():
            tmp.write(
                json.dumps({"input": {"id": vid, "price": str(price)}}).encode() + b"\n"
            )
        tmp_path = Path(tmp.name)

    res_url = staged_upload(tmp_path)
    _log("JSONL uploaded, launching bulk mutation…")

    op = gql(BULK_MUTATION, {"stagedUploadPath": res_url})["bulkOperationRunMutation"]
    if op["userErrors"]:
        raise RuntimeError(op["userErrors"])
    op_id = op["bulkOperation"]["id"]

    while True:
        status = gql(
            "{ currentBulkOperation { id status errorCode objectCount } }"
        )["currentBulkOperation"]
        _log(f"Bulk {op_id} → {status['status']}")
        if status["status"] in ("COMPLETED", "FAILED", "CANCELED"):
            break
        time.sleep(4)

    if status["status"] == "COMPLETED":
        _log(f"✅ Done — {status['objectCount']} variants processed.")
    else:
        _log(f"💥 Bulk failed: {status}")


# ─────────── BACKGROUND WORKER ──────────────────
def worker() -> None:
    try:
        _log("Fetching products…")
        products, page = [], None
        while True:
            r = requests.get(
                f"https://{SHOP_DOMAIN}/admin/api/{API_VERSION}/products.json",
                headers={"X-Shopify-Access-Token": API_TOKEN},
                params={"limit": 250, **({"page_info": page} if page else {})},
                timeout=45,
            )
            r.raise_for_status()
            products += r.json()["products"]
            page = (
                r.links.get("next", {}).get("url", "").split("page_info=")[-1] or None
            )
            if not page:
                break

        prices = load_prices()
        vmap: dict[str, float] = {}

        for p in products:
            if "CHAINE_UPDATE" not in p["tags"]:
                continue
            cat = (
                "bracelet"
                if "bracelet" in p["tags"]
                else "collier"
                if "collier" in p["tags"]
                else None
            )
            if not cat:
                continue
            base = next(
                (
                    float(m["value"])
                    for m in p.get("metafields", [])
                    if m["namespace"] == "custom" and m["key"] == "base_price"
                ),
                None,
            )
            if base is None:
                continue
            for v in p["variants"]:
                surcharge = float(prices.get(cat, {}).get(v["title"].strip(), 0))
                vmap[v["admin_graphql_api_id"]] = round(base + surcharge, 2)

        if vmap:
            bulk_update(vmap)
        else:
            _log("Nothing to update.")
    except Exception as exc:  # noqa: BLE001
        _log(f"💥 Worker crashed: {exc}")


# ─────────────── ROUTES ─────────────────────────
@app.get("/__ping")
def ping():
    return "OK", 200


@app.route("/stream")
def stream() -> Response:
    def gen():
        while True:
            yield f"data:{_log_q.get()}\n\n"

    return Response(gen(), mimetype="text/event-stream")


@app.route("/", methods=["GET", "POST"])
def prices():
    data = load_prices()
    if request.method == "POST":
        for cat in data:
            for name in data[cat]:
                field = f"{cat}_{name}"
                if field in request.form:
                    data[cat][name] = float(request.form[field])
        save_prices(data)
        flash("Surcharges saved ✔", "success")
    return render_template("prices.html", prices=data)


@app.post("/update")
def update():
    threading.Thread(target=worker, daemon=True).start()
    flash("Bulk update started — watch logs", "info")
    return redirect(url_for("prices"))


# ─────────── DEV SERVER (optional) ─────────────
if __name__ == "__main__":
    if not is_running_from_reloader():
        _log("Dev server at http://127.0.0.1:5000")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
