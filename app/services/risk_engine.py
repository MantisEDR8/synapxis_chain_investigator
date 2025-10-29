import requests
import time

# ======================
# Motor de Riesgo Synapxis — versión pública gratuita
# ======================

COVALENT_BASE = "https://api.covalenthq.com/v1"
TRONSCAN_BASE = "https://apilist.tronscanapi.com/api"
ETHERSCAN_BASE = "https://api.etherscan.io/api"
POLYGONSCAN_BASE = "https://api.polygonscan.com/api"


class RiskResult:
    def __init__(self, score=0, band="N/D", reasons=None):
        self.score = score
        self.band = band
        self.reasons = reasons or []


# --- utilidades básicas ---
def safe_get(url, params=None, headers=None):
    try:
        r = requests.get(url, params=params or {}, headers=headers or {}, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        return {}
    return {}


def classify_score(score: int) -> str:
    if score >= 70:
        return "ALTO"
    elif score >= 40:
        return "MEDIO"
    else:
        return "BAJO"


# --- consultas ligeras ---
def get_eth_tokens(address: str, chain="ethereum"):
    """Devuelve tokens y balances usando Covalent público."""
    chain_id = "1" if chain == "ethereum" else "137"
    url = f"{COVALENT_BASE}/{chain_id}/address/{address}/balances_v2/"
    data = safe_get(url)
    tokens = []
    if data and "data" in data and "items" in data["data"]:
        for item in data["data"]["items"]:
            tokens.append({
                "symbol": item.get("contract_ticker_symbol"),
                "balance": int(item.get("balance", 0)) / 10 ** int(item.get("contract_decimals", 0) or 0)
            })
    return tokens


def get_tron_account(address: str):
    """Consulta de cuenta pública en TronScan."""
    url = f"{TRONSCAN_BASE}/account"
    data = safe_get(url, params={"address": address})
    balance_trx = 0
    txs = 0
    if data and "data" in data and isinstance(data["data"], list) and len(data["data"]) > 0:
        acc = data["data"][0]
        balance_trx = acc.get("balance", 0) / 1_000_000
        txs = acc.get("totalTransactionCount", 0)
    return {"balance_trx": balance_trx, "txs": txs}


def evaluate(meta: dict, transfers: list):
    """Evalúa el riesgo cruzando datos públicos sin API privada."""
    reasons = []
    score = 0

    network = meta.get("network", "").lower()
    address = meta.get("from") or meta.get("to") or meta.get("tx_hash") or ""

    # 1️⃣ — ETH / Polygon (balances Covalent)
    if network in ("ethereum", "polygon") and address.startswith("0x"):
        tokens = get_eth_tokens(address, network)
        meta["token_count"] = len(tokens)
        meta["token_list"] = [t["symbol"] for t in tokens]
        if not tokens:
            score += 10
            reasons.append("Dirección sin tokens visibles (posible inactiva).")
        else:
            stable = [t for t in tokens if t["symbol"] in ("USDT", "USDC", "DAI")]
            if stable:
                reasons.append(f"Tiene {len(stable)} stablecoins (menor riesgo).")
            else:
                reasons.append("Sin stablecoins detectadas (mayor volatilidad).")
                score += 10

    # 2️⃣ — TRON
    elif network == "tron" and address.startswith("T"):
        acc = get_tron_account(address)
        meta["from_balance_trx"] = acc.get("balance_trx", 0)
        if acc.get("txs", 0) == 0:
            score += 20
            reasons.append("Cuenta sin transacciones registradas (inactiva).")
        else:
            reasons.append(f"{acc['txs']} transacciones detectadas en TronScan.")
        if acc.get("balance_trx", 0) > 500:
            reasons.append("Balance superior a 500 TRX (activa).")
        else:
            score += 10
            reasons.append("Balance bajo en TRX (<500).")

    # 3️⃣ — Heurística general
    if meta.get("flow_label") and "saliente" in meta["flow_label"].lower():
        score += 5
        reasons.append("Flujo saliente detectado (posible drenaje de fondos).")
    if meta.get("status") and meta["status"].lower() != "success":
        score += 10
        reasons.append("Transacción incompleta o fallida.")
    if "ponzi" in (meta.get("risk_reasons") or []):
        score += 25
        reasons.append("Coincidencia con lista Ponzi conocida.")

    # Normalización
    if score > 100:
        score = 100

    band = classify_score(score)
    return RiskResult(score, band, reasons)

