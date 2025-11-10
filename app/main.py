import os, time, logging
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, Request, Form
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

# ---- Imports de tu capa de servicios (no toques estos nombres)
from app.services.apis import (
    is_tx_hash, is_address, get_tx_receipt, get_block_info,
    parse_erc20_transfers_from_receipt, get_tron_tx,
    get_eth_address_stats, get_tron_account
)
from app.services.report import generate_docx_and_maybe_pdf
from app.services.risk_engine import evaluate as risk_evaluate

# ---------------------------
# Config & logging
# ---------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("synapxis.chain_investigator")

load_dotenv()
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "./outputs")
ENABLE_TRON = os.getenv("ENABLE_TRON", "true").lower() == "true"
ENABLE_BALANCES = os.getenv("ENABLE_BALANCES", "true").lower() == "true"

os.makedirs(OUTPUT_DIR, exist_ok=True)

app = FastAPI(title="Synapxis Chain Investigator", version="1.0.0")

# Static & templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ---------------------------
# Middleware límite de tamaño POST
# ---------------------------
MAX_INPUT_LEN = 120  # caracteres típicos (dirección/tx hash << 120)

@app.middleware("http")
async def limit_input_size(request: Request, call_next):
    if request.method == "POST":
        body = await request.body()
        if len(body) > MAX_INPUT_LEN * 10:
            return JSONResponse(
                status_code=400,
                content={"detail": f"Entrada demasiado grande (> {MAX_INPUT_LEN} chars aprox.)."},
            )
    return await call_next(request)

# ---------------------------
# Manejador global de excepciones
# ---------------------------
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logging.getLogger("uvicorn.error").exception(exc)
    return JSONResponse(
        status_code=502,
        content={"detail": "Servicio externo no disponible o error inesperado. Inténtalo de nuevo en unos minutos."},
    )

# ---------------------------
# Rutas básicas
# ---------------------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "result": None})

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/version")
def version():
    return {"app": "Synapxis Chain Investigator", "version": "1.0.0"}

# ---------------------------
# Utilidad local: resumen balances/destinos si está habilitado
# ---------------------------
def _try_get_balances_and_targets(kind: str, network: Optional[str], input_id: str) -> Dict[str, Any]:
    """
    Devuelve dict con balances y un set de destinos (otras billeteras vistas) si procede.
    No rompe si alguna API falla (gracias a safe_request en apis.py).
    """
    out: Dict[str, Any] = {"balances": None, "targets": []}

    if not ENABLE_BALANCES:
        return out

    try:
        if kind == "address":
            if (network or "").lower() in ("eth", "ethereum", "") and is_address(input_id):
                # ETH (Etherscan)
                addr_stats = get_eth_address_stats(input_id)
                if addr_stats:
                    out["balances"] = addr_stats.get("balances") or addr_stats  # flexible
                    # Extra: destinos recientes si el payload trae transfers
                    if "transfers" in addr_stats and isinstance(addr_stats["transfers"], list):
                        addrs = set()
                        for tr in addr_stats["transfers"]:
                            to_ = tr.get("to") or tr.get("to_address")
                            if to_ and to_ != input_id:
                                addrs.add(to_)
                        out["targets"] = list(addrs)

            elif ENABLE_TRON and (network or "").lower() in ("tron", "trc", "trx") and input_id.startswith("T"):
                # TRON (TronScan)
                tron_info = get_tron_account(input_id)
                if tron_info:
                    out["balances"] = tron_info
                    # TronScan no siempre da transfers por defecto; si los tuvieras, añade aquí targets
    except Exception:
        pass

    return out

# ---------------------------
# Procesamiento principal
# ---------------------------
@app.post("/analyze", response_class=HTMLResponse)
def analyze(
    request: Request,
    input_id: str = Form(...),
    kind: str = Form("auto"),         # "auto" | "address" | "tx"
    network: str = Form("auto")       # "auto" | "eth" | "tron"
):
    t0 = time.time()
    input_id = (input_id or "").strip()

    if not input_id:
        return templates.TemplateResponse("index.html", {
            "request": request,
            "result": {"summary": "Entrada vacía. Pega una dirección o hash de transacción.", "files": []}
        })

    # Autodetección
    detected_kind = None
    detected_net = None

    if kind == "address" or (kind == "auto" and is_address(input_id)):
        detected_kind = "address"
        detected_net = "eth"  # heurística simple; TRON si empieza por "T"
        if input_id.startswith("T"):
            detected_net = "tron"
    elif kind == "tx" or (kind == "auto" and is_tx_hash(input_id)):
        detected_kind = "tx"
        detected_net = "eth"
        if input_id.startswith("T"):
            detected_net = "tron"

    # Override manual si lo mandan
    if network and network.lower() in ("eth", "ethereum"):
        detected_net = "eth"
    elif network and network.lower() in ("tron", "trx", "trc"):
        detected_net = "tron"

    summary: List[str] = []
    events: List[Dict[str, Any]] = []
    transfers: List[Dict[str, Any]] = []
    meta: Dict[str, Any] = {"input": input_id, "kind": detected_kind, "network": detected_net}

    try:
        if detected_kind == "tx":
            if detected_net == "eth":
                rcpt = get_tx_receipt(input_id)
                blk = get_block_info(rcpt["blockNumber"]) if isinstance(rcpt, dict) and rcpt.get("blockNumber") else None
                if rcpt:
                    summary.append(f"TX en Ethereum | status={rcpt.get('status')} | block={rcpt.get('blockNumber')}")
                    # transfers ERC-20 si procede
                    transfers = parse_erc20_transfers_from_receipt(rcpt) or []
                    if transfers:
                        summary.append(f"Transfers ERC-20: {len(transfers)}")
                if blk:
                    summary.append(f"Bloque ts={blk.get('timestamp')}")
            elif detected_net == "tron" and ENABLE_TRON:
                trx = get_tron_tx(input_id)
                if trx:
                    summary.append(f"TX en TRON | block={trx.get('block')} | fee={trx.get('fee')}")
                    meta["tron_raw"] = trx
            else:
                summary.append("Red no soportada para TX en esta demo.")

        elif detected_kind == "address":
            if detected_net == "eth":
                # Balances/estadísticas
                enrich = _try_get_balances_and_targets("address", "eth", input_id)
                if enrich.get("balances") is not None:
                    summary.append("Balances ETH/Token obtenidos.")
                    meta["balances"] = enrich["balances"]
                if enrich.get("targets"):
                    summary.append(f"Destinos recientes detectados: {len(enrich['targets'])}")
                    meta["targets"] = enrich["targets"]

            elif detected_net == "tron" and ENABLE_TRON:
                enrich = _try_get_balances_and_targets("address", "tron", input_id)
                if enrich.get("balances") is not None:
                    summary.append("Balance TRX obtenido.")
                    meta["balances"] = enrich["balances"]
                if enrich.get("targets"):
                    summary.append(f"Destinos recientes TRON: {len(enrich['targets'])}")
                    meta["targets"] = enrich["targets"]
            else:
                summary.append("Red no soportada para ADDRESS en esta demo.")

        else:
            summary.append("No se pudo autodetectar si es dirección o transacción. Revisa el formato.")

        # Evaluación de riesgo (si procede)
        try:
            risk = risk_evaluate(meta, events, transfers)
            if risk:
                meta["risk"] = risk
                summary.append(f"Riesgo: {risk.get('score','N/A')}")
        except Exception:
            pass

        # Generar informe(s)
        files = generate_docx_and_maybe_pdf(input_id, meta, events, transfers)

        file_links = []
        for f in files:
            url = f"/download/{os.path.basename(f['path'])}"
            file_links.append({"name": f["name"], "url": url})

        t_ms = int((time.time() - t0) * 1000)
        log.info(f"Analyze done in {t_ms} ms | kind={detected_kind} | network={meta.get('network')}")

        return templates.TemplateResponse("index.html", {
            "request": request,
            "result": {"summary": " · ".join(summary) if summary else "Sin hallazgos claros.", "files": file_links}
        })

    except Exception as e:
        log.exception(e)
        return templates.TemplateResponse("index.html", {
            "request": request,
            "result": {"summary": "Error procesando el análisis. Inténtalo de nuevo.", "files": []}
        }, status_code=502)

@app.get("/analyze", response_class=HTMLResponse)
def analyze_get_redirect():
    return RedirectResponse(url="/", status_code=303)

@app.get("/download/{filename}")
def download(filename: str):
    filepath = os.path.join(OUTPUT_DIR, filename)
    return FileResponse(filepath, filename=filename)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

