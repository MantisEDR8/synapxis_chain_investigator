import os, time, logging, functools, threading
from typing import Any, Dict, Tuple, Optional, Callable
from fastapi import FastAPI, Request, Form
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

from services.apis import (
    is_tx_hash, is_address, get_tx_receipt, get_block_info,
    parse_erc20_transfers_from_receipt, get_tron_tx,
    get_eth_address_stats, get_tron_account
)
from services.report import generate_docx_and_maybe_pdf
from services.risk_engine import evaluate as risk_evaluate

# ---------------------------
# Config & logging
# ---------------------------
load_dotenv()
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "./outputs")
ENABLE_TRON = os.getenv("ENABLE_TRON", "true").lower() == "true"
ENABLE_BALANCES = os.getenv("ENABLE_BALANCES", "true").lower() == "true"
ENABLE_RISK = os.getenv("ENABLE_RISK", "true").lower() == "true"
REQUEST_TIMEOUT_S = int(os.getenv("REQUEST_TIMEOUT_S", "20"))
RETRY_ATTEMPTS = int(os.getenv("RETRY_ATTEMPTS", "2"))
CACHE_TTL_S = int(os.getenv("CACHE_TTL_S", "90"))

os.makedirs(OUTPUT_DIR, exist_ok=True)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("synapxis-main")

# ---------------------------
# App & templates
# ---------------------------
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ---------------------------
# Utils: detección y validación
# ---------------------------
def is_tron_txid(s: str) -> bool:
    return isinstance(s, str) and len(s) == 64 and not s.startswith("0x") and all(c in "0123456789abcdefABCDEF" for c in s)

def is_tron_address(s: str) -> bool:
    return isinstance(s, str) and s.startswith("T") and 34 <= len(s) <= 36

def is_tron_address(s: str) -> bool:
    return isinstance(s, str) and s.startswith("T") and 34 <= len(s) <= 36

def is_tron_address(s: str) -> bool:
    return isinstance(s, str) and s.startswith("T") and 34 <= len(s) <= 36

def normalize_input(raw: str) -> str:
    return (raw or "").strip()

def validate_input(s: str) -> Tuple[str, Optional[str]]:
    s = normalize_input(s)
    if not s:
        return "invalid", "Entrada vacía."
    if is_tx_hash(s):
        return "tx", None
    if ENABLE_TRON and is_tron_txid(s):
        return "tx_tron", None
    if is_address(s):
        return "address", None
    return "invalid", "Formato no reconocido. Use: tx Ethereum/Polygon (0x…66), address (0x…42) o tx TRON (64 hex)."

# ---------------------------
# Utils: reintentos seguros
# ---------------------------
def with_retries(fn: Callable, attempts: int = RETRY_ATTEMPTS, delay: float = 0.5):
    @functools.wraps(fn)
    def _wrap(*args, **kwargs):
        last_exc = None
        for i in range(max(1, attempts)):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                last_exc = e
                log.warning(f"Retry {i+1}/{attempts} on {fn.__name__}: {e}")
                time.sleep(delay * (i + 1))
        raise last_exc
    return _wrap

# ---------------------------
# Utils: caché ligera TTL
# ---------------------------
_cache_lock = threading.Lock()
_cache: Dict[Tuple[str, Tuple[Any, ...]], Tuple[float, Any]] = {}

def cache_ttl(ttl_s: int = CACHE_TTL_S):
    def _decor(fn):
        @functools.wraps(fn)
        def _wrap(*args, **kwargs):
            key = (fn.__name__, args + tuple(sorted(kwargs.items())))
            now = time.time()
            with _cache_lock:
                hit = _cache.get(key)
                if hit and now - hit[0] < ttl_s:
                    return hit[1]
            res = fn(*args, **kwargs)
            with _cache_lock:
                _cache[key] = (now, res)
            return res
        return _wrap
    return _decor

# ---------------------------
# Helpers de formato
# ---------------------------
def hex_to_int_safe(h: Optional[str]) -> Optional[int]:
    try:
        return int(h, 16) if isinstance(h, str) and h.startswith("0x") else None
    except Exception:
        return None

def wei_to_eth_str(v: Optional[int]) -> str:
    try:
        return f"{int(v)/1e18:.6f} ETH"
    except Exception:
        return "N/D"

# ---------------------------
# Rutas
# ---------------------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    # Página limpia: no arrastra el último result
    resp = templates.TemplateResponse("index.html", {"request": request})
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp

@app.post("/analyze", response_class=HTMLResponse)
def analyze(request: Request, input_id: str = Form(...), chain: str = Form("auto")):
        # ---- Soporte directo de DIRECCIONES TRON (early return) ----
    # Normaliza nombre de campo si tu formulario usa 'address' en lugar de 'input_id'
    _raw = locals().get("input_id") or locals().get("address")
    input_text = (_raw or "").strip()

    if is_tron_address(input_text):
        meta = {
            "network": "tron", "block": "N/A", "status": "N/A",
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            "from": input_text, "to": "N/A", "tx_hash": input_text, "flow_label": "N/D"
        }
        events, transfers = [], []

        # Datos de cuenta (TronScan gratis):
        acc = get_tron_account(input_text)
        meta["from_balance_trx"] = acc.get("balance_trx")

        # Riesgo
        risk = risk_evaluate(meta, transfers)
        meta["risk_score"], meta["risk_band"], meta["risk_reasons"] = risk.score, risk.band, risk.reasons

        summary = (
            f"Address en TRON • balance: {meta.get('from_balance_trx','N/D')} TRX"
            + (f" • riesgo: {risk.score}/100 ({risk.band})" if risk.score is not None else "")
        )

        files = generate_docx_and_maybe_pdf(input_text, meta, events, transfers)
        file_links = [{"name": f["name"], "url": f"/download/{os.path.basename(f['path'])}"} for f in files]

        return templates.TemplateResponse("index.html", {
            "request": request,
            "result": {"summary": summary, "files": file_links}
        })
    # ---- fin early-return TRON address ----

    t0 = time.time()
    kind, err = validate_input(input_id)
    if kind == "invalid":
        summary = f"Entrada inválida: {err}"
        return templates.TemplateResponse("index.html", {
            "request": request,
            "result": {"summary": summary, "files": []}
        })

    meta: Dict[str, Any] = {
        "network": "N/A", "block": "N/A", "status": "N/A", "timestamp": "N/A",
        "from": "N/A", "to": "N/A", "tx_hash": input_id, "balance": "N/A",
        "flow_label": "N/D",
    }
    events, transfers = [], []

    try:
        # ============================================================
        # ETH / POLYGON — TRANSACCIÓN
        # ============================================================
        if kind == "tx":
            rcpt, detected_chain = with_retries(get_tx_receipt)(input_id, chain=chain)
            if isinstance(rcpt, dict) and isinstance(rcpt.get("result"), dict):
                res = rcpt["result"]
                meta["network"] = detected_chain
                meta["block"] = hex_to_int_safe(res.get("blockNumber")) or "pending"
                # status
                meta["status"] = "Success" if res.get("status") == "0x1" else ("Failed" if res.get("status") == "0x0" else "Unknown")
                # from / to
                meta["from"] = res.get("from", meta["from"])
                meta["to"]   = res.get("to", meta["to"])
                # fee (si viene)
                gas_used = hex_to_int_safe(res.get("gasUsed"))
                eff_price = hex_to_int_safe(res.get("effectiveGasPrice"))
                if gas_used is not None and eff_price is not None:
                    try:
                        meta["fee_wei"] = gas_used * eff_price
                        meta["fee_eth"] = wei_to_eth_str(meta["fee_wei"])
                    except Exception:
                        pass
                # timestamp
                if res.get("blockNumber"):
                    blk = with_retries(get_block_info)(res.get("blockNumber"), detected_chain)
                    if isinstance(blk, dict) and isinstance(blk.get("result"), dict):
                        ts_hex = blk["result"].get("timestamp", "0x0")
                        ts_int = hex_to_int_safe(ts_hex) or 0
                        if ts_int:
                            meta["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts_int))

                # transfers ERC-20
                transfers = parse_erc20_transfers_from_receipt(rcpt)

                # flujo respecto al emisor
                src = (meta["from"] or "").lower()
                out_count = sum(1 for t in transfers if (t.get("from","") or "").lower() == src)
                in_count  = sum(1 for t in transfers if (t.get("to","") or "").lower() == src)
                if out_count > 0 and in_count == 0:
                    meta["flow_label"] = "saliente"
                elif in_count > 0 and out_count == 0:
                    meta["flow_label"] = "entrante"
                elif in_count > 0 or out_count > 0:
                    meta["flow_label"] = "mixto"
                else:
                    meta["flow_label"] = "desconocido"

                # balances (opcional)
                if ENABLE_BALANCES:
                    @cache_ttl()
                    def _stats(addr: str, ch: str): return get_eth_address_stats(addr, ch)
                    if isinstance(meta["from"], str) and meta["from"].startswith("0x") and len(meta["from"]) == 42:
                        s_from = with_retries(_stats)(meta["from"], detected_chain)
                        meta["from_balance_wei"] = s_from.get("balance_wei")
                        meta["from_nonce"] = s_from.get("nonce")
                    if isinstance(meta["to"], str) and meta["to"].startswith("0x") and len(meta["to"]) == 42:
                        s_to = with_retries(_stats)(meta["to"], detected_chain)
                        meta["to_balance_wei"] = s_to.get("balance_wei")
                        meta["to_nonce"] = s_to.get("nonce")

                # evento principal
                events.append({
                    "ts": meta["timestamp"], "type": "tx_confirmed", "tx_hash": input_id,
                    "notes": f"Block {meta['block']}", "from": meta["from"], "to": meta["to"],
                    "contract": meta["to"], "amount_raw": ""
                })

                # riesgo
                if ENABLE_RISK:
                    risk = risk_evaluate(meta, transfers)
                    meta["risk_score"], meta["risk_band"], meta["risk_reasons"] = risk.score, risk.band, risk.reasons

                summary = (
                    f"Tx en {meta['network']} • estado: {meta['status']} • bloque: {meta['block']} "
                    f"• flujo: {meta['flow_label']} • transfers: {len(transfers)}"
                    + (f" • fee: {meta.get('fee_eth')}" if meta.get("fee_eth") else "")
                    + (f" • riesgo: {meta.get('risk_score')}/100 ({meta.get('risk_band')})" if meta.get("risk_score") is not None else "")
                )
            else:
                summary = "No se pudo obtener el recibo (API V2, rate-limit o tx inexistente)."
        # ============================================================
        # TRON — TRANSACCIÓN
        # ============================================================
        elif kind == "tx_tron":
            if not ENABLE_TRON:
                summary = "TRON deshabilitado (ENABLE_TRON=false)."
            else:
                tx = with_retries(get_tron_tx)(input_id)
                if tx:
                    meta["network"] = "tron"
                    meta["block"] = tx.get("block", "N/A")
                    try:
                        ts_ms = int(tx.get("timestamp", 0))
                        meta["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts_ms // 1000))
                    except Exception:
                        pass
                    meta["status"] = "Success" if tx.get("confirmed") else "Pending/Unknown"
                    meta["from"] = tx.get("from", "N/A")
                    meta["to"] = tx.get("to", "N/A")
                    if isinstance(tx.get("fee"), (int, float)):
                        meta["fee_trx"] = f"{float(tx['fee'])/1_000_000:.6f} TRX"  # SUN→TRX aprox

                    # flujo (básico)
                    meta["flow_label"] = "saliente" if meta["from"] and meta["to"] else "desconocido"

                    # balances TRON (opcional)
                    if ENABLE_BALANCES:
                        @cache_ttl()
                        def _tacc(addr: str): return get_tron_account(addr)
                        if isinstance(meta["from"], str) and meta["from"].startswith("T"):
                            a_from = with_retries(_tacc)(meta["from"])
                            meta["from_balance_trx"] = a_from.get("balance_trx")
                        if isinstance(meta["to"], str) and meta["to"].startswith("T"):
                            a_to = with_retries(_tacc)(meta["to"])
                            meta["to_balance_trx"] = a_to.get("balance_trx")

                    events.append({
                        "ts": meta["timestamp"], "type": "tx_confirmed", "tx_hash": input_id,
                        "notes": f"Block {meta['block']}", "from": meta["from"], "to": meta["to"],
                        "contract": meta["to"], "amount_raw": ""
                    })
                    transfers = []  # TRC-20 parsing vendrá aparte

                    if ENABLE_RISK:
                        risk = risk_evaluate(meta, transfers)
                        meta["risk_score"], meta["risk_band"], meta["risk_reasons"] = risk.score, risk.band, risk.reasons

                    summary = (
                        f"Tx en TRON • estado: {meta['status']} • bloque: {meta['block']} "
                        f"• flujo: {meta['flow_label']}"
                        + (f" • fee: {meta.get('fee_trx')}" if meta.get("fee_trx") else "")
                        + (f" • riesgo: {meta.get('risk_score')}/100 ({meta.get('risk_band')})" if meta.get("risk_score") is not None else "")
                    )
                else:
                    summary = "No se encontró la transacción en TronScan API."
        # ============================================================
        # ETH / POLYGON — ADDRESS
        # ============================================================
        elif kind == "address":
            detected_chain = "ethereum" if chain not in ("polygon",) else "polygon"
            meta["network"] = detected_chain
            meta["status"] = "N/A"
            meta["from"] = input_id
            meta["to"] = "N/A"
            meta["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
            transfers = []

            if ENABLE_BALANCES:
                @cache_ttl()
                def _stats(addr: str, ch: str): return get_eth_address_stats(addr, ch)
                s = with_retries(_stats)(input_id, detected_chain)
                meta["from_balance_wei"] = s.get("balance_wei")
                meta["from_nonce"] = s.get("nonce")

            events.append({
                "ts": meta["timestamp"], "type": "address_scan", "tx_hash": "",
                "notes": f"Address scan en {detected_chain}", "from": input_id,
                "to": "", "contract": "", "amount_raw": ""
            })

            if ENABLE_RISK:
                risk = risk_evaluate(meta, transfers)
                meta["risk_score"], meta["risk_band"], meta["risk_reasons"] = risk.score, risk.band, risk.reasons

            bal_str = wei_to_eth_str(meta.get("from_balance_wei")) if meta.get("from_balance_wei") is not None else "N/D"
            summary = (
                f"Address en {detected_chain} • balance: {bal_str} • nonce: {meta.get('from_nonce','N/D')}"
                + (f" • riesgo: {meta.get('risk_score')}/100 ({meta.get('risk_band')})" if meta.get("risk_score") is not None else "")
            )
        else:
            summary = "Entrada inválida."
    except Exception as e:
        log.exception(f"Error en analyze: {e}")
        summary = f"Error durante el análisis: {type(e).__name__}: {e}"

    # Generar informe(s)
    files = generate_docx_and_maybe_pdf(input_id, meta, events, transfers)

    # Links de descarga
    file_links = []
    for f in files:
        url = f"/download/{os.path.basename(f['path'])}"
        file_links.append({"name": f["name"], "url": url})

    t_ms = int((time.time() - t0) * 1000)
    log.info(f"Analyze done in {t_ms} ms | kind={kind} | network={meta.get('network')}")

    return templates.TemplateResponse("index.html", {
        "request": request,
        "result": {"summary": summary, "files": file_links}
    })

@app.get("/analyze", response_class=HTMLResponse)
def analyze_get_redirect():
    # Si el usuario recarga /analyze con GET, redirige a la home
    return RedirectResponse(url="/", status_code=303)

@app.get("/download/{filename}")
def download(filename: str):
    filepath = os.path.join(OUTPUT_DIR, filename)
    return FileResponse(filepath, filename=filename)
if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)


