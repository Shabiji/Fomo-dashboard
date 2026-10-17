#!/usr/bin/env python3
"""Rebuild index.html from copies.json and public data.

Reads copies.json (never writes it). Fetches the wallet's USDC balance and
recent signatures from the public Solana RPC, and each handle's PnL from
provadata.com. Writes index.html only when the page data changed, or when
the footer timestamp is at least an hour old.
"""

import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COPIES_PATH = ROOT / "copies.json"
INDEX_PATH = ROOT / "index.html"

RPC_URL = "https://api.mainnet-beta.solana.com"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
FAIL = "Couldn't fetch right now"
USER_AGENT = "fomo-dashboard/1.0"

# Transfers, account setup, and memos are funding activity, not copied trades.
PLAIN_PROGRAMS = {
    "11111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
    "ComputeBudget111111111111111111111111111111",
}

FOOTER_RE = re.compile(
    r"<footer>Data as of .*? · real data only</footer>"
)
STAMP_RE = re.compile(
    r"<footer>Data as of (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC · real data only</footer>"
)
PNL_RE = {
    "24h": re.compile(r"PnL 24h</span>\s*<strong[^>]*>\s*([^<]+?)\s*</strong>", re.I),
    "7d": re.compile(r"PnL 7d</span>\s*<strong[^>]*>\s*([^<]+?)\s*</strong>", re.I),
    "30d": re.compile(r"PnL 30d</span>\s*<strong[^>]*>\s*([^<]+?)\s*</strong>", re.I),
}
AS_OF_RE = re.compile(r"as of (\d{1,2} [A-Za-z]+ \d{4}, \d{2}:\d{2} UTC)")
UPDATED_RE = re.compile(r"updated (\d{1,2} [A-Za-z]+ \d{4}, \d{2}:\d{2} UTC)")

STYLE = """:root{--bg:#0b0f0d;--card:#121815;--line:#2a332f;--txt:#f2f5f3;--mut:#8a948f;--green:#3ddc97;--amber:#f5c542;--red:#ff5f6d;--blue:#8b9cff;
--sans:-apple-system,system-ui,Roboto,"Segoe UI",sans-serif;--mono:ui-monospace,"SFMono-Regular",Menlo,Consolas,monospace}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font-family:var(--sans);line-height:1.45}
.wrap{max-width:560px;margin:0 auto;padding:20px 16px 40px}
header{display:flex;align-items:center;gap:10px;padding:6px 0 18px;border-bottom:1px solid var(--line);margin-bottom:18px}
.dot{width:10px;height:10px;border-radius:50%;background:var(--green);box-shadow:0 0 10px var(--green)}
h1{font-size:22px;margin:0;font-weight:800;letter-spacing:-.3px}h1 span{color:var(--mut);font-weight:400}h1 b{color:var(--green)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:14px}
h2{font-size:12px;text-transform:uppercase;letter-spacing:1.2px;color:var(--mut);margin:0 0 12px;font-weight:600}
.handle{font-family:var(--mono);font-size:20px;font-weight:700;color:var(--green)}
.row{display:flex;justify-content:space-between;padding:8px 0;border-top:1px solid var(--line);font-size:15px}
.row:first-of-type{border-top:0}.row .k{color:var(--mut)}.row .v{font-family:var(--mono);font-weight:600}
.pos{color:var(--green)}.neg{color:var(--red)}
.note{margin-top:12px;padding:10px 12px;border-radius:10px;background:rgba(245,197,66,.1);border:1px solid rgba(245,197,66,.35);color:var(--amber);font-size:14px}
.warn{margin-top:12px;padding:10px 12px;border-radius:10px;background:rgba(255,95,109,.1);border:1px solid rgba(255,95,109,.35);color:var(--red);font-size:14px}
.big{font-family:var(--mono);font-size:30px;font-weight:800}.sub{color:var(--mut);font-size:14px;margin-top:4px}
.pill{display:inline-block;font-size:12px;padding:3px 9px;border-radius:99px;background:rgba(61,220,151,.12);color:var(--green);border:1px solid rgba(61,220,151,.35);margin-left:6px;vertical-align:middle}
a{color:var(--blue);text-decoration:none}
footer{color:var(--mut);font-size:12px;text-align:center;margin-top:20px}"""


def esc(value):
    return html.escape(str(value), quote=True)


def fetch_bytes(url, data=None, headers=None, timeout=30):
    req_headers = {"User-Agent": USER_AGENT}
    if headers:
        req_headers.update(headers)
    last_error = None
    for attempt in range(2):
        try:
            request = urllib.request.Request(url, data=data, headers=req_headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read(), response.headers.get_content_charset()
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            last_error = error
            if attempt == 0:
                time.sleep(1)
    raise last_error


def fetch_text(url):
    raw, charset = fetch_bytes(url)
    return raw.decode(charset or "utf-8", errors="replace")


def rpc(method, params):
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode()
    raw, _charset = fetch_bytes(
        RPC_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    body = json.loads(raw.decode("utf-8"))
    if not isinstance(body, dict) or "error" in body or "result" not in body:
        raise ValueError("solana rpc error")
    return body["result"]


def rpc_batch(calls):
    payload = json.dumps(
        [
            {"jsonrpc": "2.0", "id": index, "method": method, "params": params}
            for index, (method, params) in enumerate(calls)
        ]
    ).encode()
    raw, _charset = fetch_bytes(
        RPC_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    body = json.loads(raw.decode("utf-8"))
    if isinstance(body, dict):
        body = [body]
    if not isinstance(body, list):
        raise ValueError("solana rpc batch error")
    by_id = {}
    for item in body:
        if not isinstance(item, dict) or "id" not in item:
            raise ValueError("solana rpc batch error")
        by_id[item["id"]] = item
    results = []
    for index in range(len(calls)):
        item = by_id.get(index)
        if item is None or "error" in item or "result" not in item:
            results.append(None)
        else:
            results.append(item["result"])
    return results


def format_usdc(amount):
    quantized = amount.quantize(Decimal("0.000001"))
    sign = "-" if quantized < 0 else ""
    quantized = abs(quantized)
    whole, frac = f"{quantized:.6f}".split(".")
    frac = frac.rstrip("0")
    if len(frac) < 2:
        frac = frac.ljust(2, "0")
    return f"{sign}{int(whole):,}.{frac}"


def format_block_time(block_time):
    moment = datetime.fromtimestamp(block_time, timezone.utc)
    return moment.strftime("%d %b %Y, %H:%M UTC").lstrip("0")


def short_sig(signature):
    if len(signature) <= 12:
        return signature
    return signature[:4] + "…" + signature[-4:]


def value_class(value):
    text = str(value).strip()
    if text.startswith("+"):
        return " pos"
    if text.startswith(("-", "–", "−")):
        return " neg"
    return ""


def row(label, value):
    return (
        f'<div class="row"><span class="k">{esc(label)}</span>'
        f'<span class="v{value_class(value)}">{esc(value)}</span></div>'
    )


def load_copies():
    data = json.loads(COPIES_PATH.read_text(encoding="utf-8"))
    wallet = data["wallet"]
    next_review = data["next_review_utc"]
    copies = data["copies"]
    if not isinstance(wallet, str) or not wallet:
        raise ValueError("copies.json wallet is missing")
    if not isinstance(next_review, str) or not next_review.strip():
        raise ValueError("copies.json next_review_utc is missing")
    if not isinstance(copies, list) or not copies:
        raise ValueError("copies.json copies is missing")
    for copy in copies:
        if not isinstance(copy, dict) or not str(copy.get("handle") or "").strip():
            raise ValueError("copies.json has a copy without a handle")
    return wallet, next_review.strip(), copies


def fetch_balance(wallet):
    result = rpc(
        "getTokenAccountsByOwner",
        [wallet, {"mint": USDC_MINT}, {"encoding": "jsonParsed"}],
    )
    accounts = result.get("value")
    if not isinstance(accounts, list):
        raise ValueError("unexpected balance result")
    total = Decimal(0)
    for account in accounts:
        info = (
            account.get("account", {})
            .get("data", {})
            .get("parsed", {})
            .get("info", {})
        )
        token = info.get("tokenAmount") or {}
        if token.get("uiAmountString") is not None:
            total += Decimal(str(token["uiAmountString"]))
        elif token.get("amount") is not None and token.get("decimals") is not None:
            total += Decimal(token["amount"]) / (Decimal(10) ** int(token["decimals"]))
        else:
            raise ValueError("token amount missing")
    return total


def fetch_signatures(wallet):
    result = rpc("getSignaturesForAddress", [wallet, {"limit": 10}])
    if not isinstance(result, list):
        raise ValueError("unexpected signature result")
    return result[:10]


def program_ids(transaction):
    message = (transaction.get("transaction") or {}).get("message") or {}
    groups = [message.get("instructions") or []]
    for inner in (transaction.get("meta") or {}).get("innerInstructions") or []:
        groups.append(inner.get("instructions") or [])
    found = []
    for instructions in groups:
        for instruction in instructions:
            if isinstance(instruction, dict) and instruction.get("programId"):
                found.append(instruction["programId"])
    return found


def usdc_and_other_deltas(transaction, wallet):
    meta = transaction.get("meta") or {}

    def side(name):
        totals = {}
        for entry in meta.get(name) or []:
            if entry.get("owner") != wallet:
                continue
            mint = entry.get("mint")
            token = entry.get("uiTokenAmount") or {}
            if mint is None or token.get("uiAmountString") is None:
                raise ValueError("token balance missing")
            totals[mint] = totals.get(mint, Decimal(0)) + Decimal(
                str(token["uiAmountString"])
            )
        return totals

    pre = side("preTokenBalances")
    post = side("postTokenBalances")
    deltas = {}
    for mint in set(pre) | set(post):
        deltas[mint] = post.get(mint, Decimal(0)) - pre.get(mint, Decimal(0))
    return deltas


def classify_transaction(transaction, wallet):
    """Return 'deposit', 'trade', or 'other'. Raise if the payload is unusable."""
    if not transaction:
        raise ValueError("missing transaction")
    deltas = usdc_and_other_deltas(transaction, wallet)
    other = [
        mint
        for mint, delta in deltas.items()
        if mint != USDC_MINT and delta != 0
    ]
    if other or any(program not in PLAIN_PROGRAMS for program in program_ids(transaction)):
        return "trade"
    if deltas.get(USDC_MINT, Decimal(0)) > 0:
        return "deposit"
    return "other"


def fetch_trades(signatures, wallet):
    """Return (kinds, failed). kinds aligns with signatures; None means that tx failed."""
    if not signatures:
        return [], False
    calls = []
    for entry in signatures:
        signature = entry.get("signature")
        if not signature:
            return [None] * len(signatures), True
        calls.append(
            (
                "getTransaction",
                [
                    signature,
                    {
                        "encoding": "jsonParsed",
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
            )
        )
    try:
        results = rpc_batch(calls)
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError, KeyError):
        return [None] * len(signatures), True
    kinds = []
    failed = False
    for transaction in results:
        try:
            kinds.append(classify_transaction(transaction, wallet))
        except (ValueError, KeyError, TypeError, ArithmeticError):
            kinds.append(None)
            failed = True
    return kinds, failed


def fetch_review(handle):
    page = fetch_text(f"https://provadata.com/traders/{urllib.request.quote(handle)}")
    values = {}
    for label, pattern in PNL_RE.items():
        match = pattern.search(page)
        if not match:
            values[label] = None
            continue
        text = html.unescape(match.group(1)).strip()
        values[label] = text if text and len(text) <= 40 else None
    captured = None
    as_of = AS_OF_RE.search(html.unescape(page))
    updated = UPDATED_RE.search(html.unescape(page))
    if as_of:
        captured = as_of.group(1)
    elif updated:
        captured = updated.group(1)
    return values, captured


def copy_card(copy):
    handle = str(copy["handle"])
    parts = [
        '<section class="card"><h2>Active copy</h2>',
        f'<div class="handle">@{esc(handle)} <span class="pill">Copying</span></div>',
        '<div style="margin-top:10px">',
        row("Size", copy.get("size") if copy.get("size") is not None else FAIL),
        row("Mode", copy.get("mode") if copy.get("mode") is not None else FAIL),
        row("Stop-loss", copy.get("stop_loss") if copy.get("stop_loss") is not None else FAIL),
        row("Take-profit", copy.get("take_profit") if copy.get("take_profit") is not None else FAIL),
        row("Chains", copy.get("chains") if copy.get("chains") is not None else FAIL),
        "</div>",
    ]
    if copy.get("note"):
        parts.append(f'<div class="note">{esc(copy["note"])}</div>')
    if copy.get("warning"):
        parts.append(f'<div class="warn">{esc(copy["warning"])}</div>')
    parts.append("</section>")
    return "".join(parts)


def review_card(handle, pnl, captured):
    captured_text = f"Captured {captured}" if captured else FAIL
    rows = (
        row("24 hours", pnl.get("24h") or FAIL)
        + row("7 days", pnl.get("7d") or FAIL)
        + row("30 days", pnl.get("30d") or FAIL)
    )
    return (
        '<section class="card"><h2>Latest review</h2>'
        f'<div class="handle">@{esc(handle)}</div>'
        '<div style="margin-top:10px">'
        + rows
        + "</div>"
        f'<div class="sub" style="margin-top:10px">{esc(captured_text)}</div>'
        "</section>"
    )


def signature_rows(signatures):
    rows = []
    for entry in signatures:
        signature = entry.get("signature")
        block_time = entry.get("blockTime")
        if not signature:
            rows.append(row("Transaction", FAIL))
            continue
        when = format_block_time(block_time) if isinstance(block_time, int) else FAIL
        href = "https://solscan.io/tx/" + urllib.request.quote(signature)
        label = (
            f'<a href="{esc(href)}" title="{esc(signature)}">{esc(short_sig(signature))}</a>'
        )
        rows.append(
            f'<div class="row"><span class="k">{label}</span>'
            f'<span class="v">{esc(when)}</span></div>'
        )
    return "".join(rows)


def wallet_card(balance, signatures, kinds, details_failed, balance_failed, sigs_failed):
    if balance_failed:
        balance_html = f'<div class="big" style="font-size:22px">{esc(FAIL)}</div>'
    else:
        balance_html = (
            f'<div class="big">{esc(format_usdc(balance))} '
            '<span style="font-size:16px;color:var(--mut)">USDC</span></div>'
        )
    parts = ['<section class="card"><h2>Wallet</h2>', balance_html]
    if sigs_failed:
        parts.append(f'<div class="sub">{esc(FAIL)}</div>')
    else:
        trades = [kind for kind in kinds if kind == "trade"]
        unknown = details_failed or any(kind is None for kind in kinds)
        if not signatures or (not trades and not unknown):
            parts.append('<div class="sub">No copied trades yet.</div>')
        elif unknown and not trades:
            parts.append(f'<div class="sub">{esc(FAIL)}</div>')
        if signatures:
            parts.append('<div class="sub" style="margin-top:12px">Recent transactions</div>')
            parts.append('<div style="margin-top:10px">')
            parts.append(signature_rows(signatures))
            parts.append("</div>")
    parts.append("</section>")
    return "".join(parts)


def next_review_card(text):
    if text.endswith(" UTC"):
        main = text[:-4]
        body = (
            f'<div class="big" style="font-size:24px">{esc(main)} '
            '<span style="font-size:16px;color:var(--mut)">UTC</span></div>'
        )
    else:
        body = f'<div class="big" style="font-size:24px">{esc(text)}</div>'
    return f'<section class="card"><h2>Next review</h2>{body}</section>'


def render(wallet_html, copies, reviews, next_review, stamp):
    cards = [copy_card(copy) for copy in copies]
    cards.append(wallet_html)
    for handle, pnl, captured in reviews:
        cards.append(review_card(handle, pnl, captured))
    cards.append(next_review_card(next_review))
    footer = f"Data as of {stamp} · real data only"
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        "<title>FOMO Bot × dawn</title>\n"
        "<style>\n"
        f"{STYLE}\n"
        "</style></head><body><div class=\"wrap\">\n"
        '<header><div class="dot"></div><h1>FOMO Bot <span>×</span> <b>dawn</b></h1></header>\n\n'
        + "\n\n".join(cards)
        + f"\n\n<footer>{footer}</footer>\n"
        "</div></body></html>\n"
    )


def should_write(new_html, old_html, now):
    if old_html is None:
        return True
    if FOOTER_RE.sub("<footer>STAMP</footer>", new_html) != FOOTER_RE.sub(
        "<footer>STAMP</footer>", old_html
    ):
        return True
    match = STAMP_RE.search(old_html)
    if not match:
        return True
    previous = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc
    )
    return now - previous >= timedelta(hours=1)


def main():
    try:
        wallet, next_review, copies = load_copies()
    except (OSError, json.JSONDecodeError, ValueError, KeyError) as error:
        print(f"copies.json: {error}", file=sys.stderr)
        return 1

    balance = None
    balance_failed = False
    try:
        balance = fetch_balance(wallet)
        print(f"balance: {format_usdc(balance)} USDC")
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, json.JSONDecodeError, ArithmeticError) as error:
        balance_failed = True
        print(f"balance: {FAIL} ({error})")

    signatures = []
    sigs_failed = False
    try:
        signatures = fetch_signatures(wallet)
        print(f"signatures: {len(signatures)}")
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, json.JSONDecodeError) as error:
        sigs_failed = True
        print(f"signatures: {FAIL} ({error})")

    kinds = []
    details_failed = False
    if not sigs_failed:
        kinds, details_failed = fetch_trades(signatures, wallet)
        for entry, kind in zip(signatures, kinds):
            print(f"tx {entry.get('signature')}: {kind or FAIL}")

    reviews = []
    for copy in copies:
        handle = str(copy["handle"])
        try:
            pnl, captured = fetch_review(handle)
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as error:
            pnl, captured = {"24h": None, "7d": None, "30d": None}, None
            print(f"{handle}: {FAIL} ({error})")
        else:
            for label in ("24h", "7d", "30d"):
                print(f"{handle} {label}: {pnl.get(label) or FAIL}")
            print(f"{handle} captured: {captured or FAIL}")
        reviews.append((handle, pnl, captured))

    now = datetime.now(timezone.utc).replace(microsecond=0)
    stamp = now.strftime("%Y-%m-%d %H:%M:%S UTC")
    new_html = render(
        wallet_card(balance, signatures, kinds, details_failed, balance_failed, sigs_failed),
        copies,
        reviews,
        next_review,
        stamp,
    )
    old_html = INDEX_PATH.read_text(encoding="utf-8") if INDEX_PATH.exists() else None
    if should_write(new_html, old_html, now):
        INDEX_PATH.write_text(new_html, encoding="utf-8")
        print("wrote: index.html")
    else:
        print("wrote: no (data unchanged)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
