"""
shopify-price-dash/app.py
Flask dashboard + Shopify bulk price updater using GraphQL + live SSE logs.
"""

import json
import os
import queue
import tempfile
import threading
import time
from collections import deque
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

# ─── CONFIG ───────────────────────────────────────────
load_dotenv()  # loads .env in dev or ENV vars in prod

SHOP_DOMAIN  = os.getenv("SHOP_DOMAIN")
API_TOKEN    = os.getenv("API_TOKEN")
FLASK_SECRET = os.getenv("FLASK_SECRET", "change-me")
API_VERSION  = "2024-04"
GRAPHQL_URL  = f"https://{SHOP_DOMAIN}/admin/api/{API_VERSION}/graphql.json"

if not SHOP_DOMAIN or not API_TOKEN:
    raise RuntimeError("Set SHOP_DOMAIN and API_TOKEN in env!")

HEADERS_GQL = {
    "X-Shopify-Access-Token": API_TOKEN,
    "Content-Type": "application/json",
}

SURCHARGE_FILE = "variant_prices.json"

# ─── FLASK SETUP ─────────────────────────────────────
app = Flask(__name__)
app.secret_key = FLASK_SECRET

# ring buffer + queue for live logs (SSE)
log_buffer: deque[str] = deque(maxlen=200)
_log_q: "queue.SimpleQueue[str]" = queue.SimpleQueue()

def _log(msg: str):
    """Add timestamped message to buffer + SSE queue."""
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    log_buffer.append(line)
    _log_q.put_nowait(line)


# ─── UTILITIES ───────────────────────────────────────
def load_prices() -> dict[str, Any]:
    with open(SURCHARGE_FILE, "r", encoding="utf-8") as fh:
        return json.load(fh)

def save_prices(data: dict[str, Any]):
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


# ─── GRAPHQL FETCH ────────────────────────────────────
def fetch_products_graphql() -> list[dict]:
    _log("Fetching products via GraphQL…")
    products: list[dict] = []
    cursor = None

    graphql_query = """
    query fetchProducts($cursor: String) {
      products(first: 250, query: "tag:CHAINE_UPDATE", after: $cursor) {
        pageInfo { hasNextPage endCursor }
        edges {
          node {
            id
            tags
            metafields(first: 10, namespace: "custom") {
              edges { node { key value } }
            }
            variants(first: 100) {
              edges { node { id title } }
            }
          }
        }
      }
    }
    """

    while True:
        data = gql(graphql_query, {"cursor": cursor})["products"]
        for edge in data["edges"]:
            products.append(edge["node"])
        if not data["pageInfo"]["hasNextPage"]:
            break
        cursor = data["pageInfo"]["endCursor"]

    _log(f"Fetched {len(products)} products via GraphQL")
    for p in products[:3]:
        _log(f"DEBUG: product {p['id']} tags={p['tags']} metafields={p['metafields']['edges']}")
    return products


# ─── BULK MUTATION HELPERS ────────────────────────────
BULK_MUTATION = r"""
mutation bulk($stagedUploadPath: String!) {
  bulkOperationRunMutation(
    mutation: \"""" + r"""
mutation call($input: ProductVariantInput!) {
  productVariantUpdate(input: $input) { userErrors { field message } }
}
""" + r"""\""",
    stagedUploadPath: $stagedUploadPath
  ) { bulkOperation { id status } userErrors { field message } }
}
"""

def staged_upload(jsonl_path: Path) -> str:
    resp = gql(
        """
    mutation($input:[StagedUploadInput!]!){
      stagedUploadsCreate(input:$input){
        stagedTargets {
          url
          resourceUrl
          parameters { name value }
        }
        userErrors { field message }
      }
    }
    """,
        {"input": [{
            "resource":   "BULK_MUTATION_VARIABLES",
            "filename":   jsonl_path.name,
            "mimeType":   "text/jsonl",
            "httpMethod": "PUT",
        }]},
    )["stagedUploadsCreate"]

    if resp["userErrors"]:
        raise RuntimeError(resp["userErrors"])

    tgt = resp["stagedTargets"][0]
    with open(jsonl_path, "rb") as fh:
        requests.put(
            tgt["url"],
            params={p["name"]: p["value"] for p in tgt["parameters"]},
            data=fh,
            headers={"Content-Type": "text/jsonl"},
            timeout=120,
        ).raise_for_status()
    return tgt["resourceUrl"]

def bulk_update(variant_map: dict[str, float]):
    _log(f"Preparing {len(variant_map)} variants…")
    with tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl") as tmp:
        for vid, price in variant_map.items():
            tmp.write(json.dumps({"input": {"id": vid, "price": str(price)}}).encode() + b"\n")
        tmp_path = Path(tmp.name)

    res_url = staged_upload(tmp_path)
    _log("JSONL uploaded, launching bulk mutation…")

    op = gql(BULK_MUTATION, {"stagedUploadPath": res_url})["bulkOperationRunMutation"]
    if op["userErrors"]:
        raise RuntimeError(op["userErrors"])
    op_id = op["bulkOperation"]["id"]

    while True:
        stat = gql("{ currentBulkOperation { id status errorCode objectCount } }")["currentBulkOperation"]
        _log(f"Bulk {op_id} → {stat['status']}")
        if stat["status"] in ("COMPLETED", "FAILED", "CANCELED"):
            break
        time.sleep(4)

    if stat["status"] == "COMPLETED":
        _log(f"✅ Completed — {stat['objectCount']} variants.")
    else:
        _log(f"💥 Bulk failed: {stat}")


# ─── BACKGROUND WORKER ────────────────────────────────
def worker():
    try:
        products = fetch_products_graphql()
        _log(f"DEBUG: total products in worker = {len(products)}")
        prices = load_prices()
        variant_map: dict[str, float] = {}

        for p in products:
            # category by tag
            if "bracelet" in p["tags"]:
                cat = "bracelet"
            elif "collier" in p["tags"]:
                cat = "collier"
            else:
                _log(f"DEBUG: skipping {p['id']}—no bracelet/collier tag")
                continue

            # find base_price
            base = None
            for mf in p["metafields"]["edges"]:
                if mf["node"]["key"] == "base_price":
                    base = float(mf["node"]["value"])
                    break
            if base is None:
                _log(f"DEBUG: skipping {p['id']}—no custom.base_price metafield")
                continue

            for v_edge in p["variants"]["edges"]:
                v = v_edge["node"]
                surcharge = float(prices.get(cat, {}).get(v["title"].strip(), 0))
                variant_map[v["id"]] = round(base + surcharge, 2)

        if variant_map:
            bulk_update(variant_map)
        else:
            _log("Nothing to update.")
    except Exception as e:
        _log(f"💥 Worker crashed: {e}")


# ─── ROUTES & SSE ─────────────────────────────────────
@app.get("/__ping")
def ping():
    return "OK", 200

@app.route("/stream")
def stream():
    def gen():
        for l in list(log_buffer):
            yield f"data:{l}\n\n"
        while True:
            yield f"data:{_log_q.get()}\n\n"
    return Response(gen(), mimetype="text/event-stream")

@app.route("/", methods=["GET", "POST"])
def prices():
    data = load_prices()
    if request.method == "POST":
        for cat in data:
            for name in data[cat]:
                f = f"{cat}_{name}"
                if f in request.form:
                    data[cat][name] = float(request.form[f])
        save_prices(data)
        flash("Surcharges saved ✔", "success")
    return render_template("prices.html", prices=data)

@app.post("/update")
def update():
    _log("👋 /update called – launching worker")
    threading.Thread(target=worker, daemon=True).start()
    flash("Bulk update started — watch logs", "info")
    return redirect(url_for("prices"))

if __name__ == "__main__":
    if not is_running_from_reloader():
        _log("Dev server ready at http://127.0.0.1:5000")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
