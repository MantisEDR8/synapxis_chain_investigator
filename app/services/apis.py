import os
import requests
from dotenv import load_dotenv

import logging

logger = logging.getLogger(__name__)

def safe_request(func):
    """Decorador para capturar errores de red o respuesta"""
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except requests.exceptions.Timeout:
            logger.error(f"Timeout en {func.__name__}")
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"Error de red en {func.__name__}: {e}")
            return None
        except Exception as e:
            logger.error(f"Error inesperado en {func.__name__}: {e}")
            return None
    return wrapper

# Límite de tamaño de entrada para evitar abusos
MAX_INPUT_LEN = 120
if len(input_id) > MAX_INPUT_LEN:
    return templates.TemplateResponse("index.html", {
        "request": request,
        "result": {"summary": f"Entrada demasiado larga (> {MAX_INPUT_LEN} caracteres). Verifica la dirección o hash.", "files": []}
    }, status_code=400)

load_dotenv()

DEFAULT_TIMEOUT = 15  # segundos
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY")

def http_get(url, **kwargs):
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    return http_get(url, **kwargs)

def http_post(url, **kwargs):
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    return http_post(url, **kwargs)

# ---------------------------
# Detectores básicos
# ---------------------------
def is_tx_hash(value: str) -> bool:
    return isinstance(value, str) and value.startswith("0x") and len(value) == 66

def is_address(value: str) -> bool:
    return isinstance(value, str) and value.startswith("0x") and len(value) == 42

def _chainid_for(chain: str) -> int:
    """
    Etherscan V2 Multichain: Ethereum=1, Polygon=137
    """
    return 137 if chain == "polygon" else 1

# ---------------------------
# Etherscan V2 (ETH / Polygon): TX, BLOQUE
# ---------------------------
def get_tx_receipt(txhash: str, chain: str = "auto"):
    """
    Usa Etherscan V2 Multichain e intenta Polygon primero si chain=auto.
    Devuelve (json, 'polygon'|'ethereum')
    """
    if chain in ("auto", "polygon"):
        data = _etherscan_v2_proxy_receipt(txhash, chain="polygon")
        if isinstance(data, dict) and isinstance(data.get("result"), dict) and data["result"].get("blockNumber"):
            return data, "polygon"
    data = _etherscan_v2_proxy_receipt(txhash, chain="ethereum")
    return data, "ethereum"

def _etherscan_v2_proxy_receipt(txhash: str, chain: str):
    base = "https://api.etherscan.io/v2/api"
    params = {
        "chainid": _chainid_for(chain),
        "module": "proxy",
        "action": "eth_getTransactionReceipt",
        "txhash": txhash,
        "apikey": ETHERSCAN_API_KEY
    }
    r = http_get(base, params=params, timeout=20)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return {"status": "0", "message": "non_json_response", "result": None}

def get_block_info(block_number_hex: str, chain: str):
    base = "https://api.etherscan.io/v2/api"
    params = {
        "chainid": _chainid_for(chain),
        "module": "proxy",
        "action": "eth_getBlockByNumber",
        "tag": block_number_hex,
        "boolean": "true",
        "apikey": ETHERSCAN_API_KEY
    }
    r = http_get(base, params=params, timeout=20)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return {"status": "0", "message": "non_json_response", "result": None}

# ---------------------------
# Etherscan V2 (ETH / Polygon): BALANCE y ACTIVIDAD
# ---------------------------
def _etherscan_v2_proxy_eth_getBalance(address: str, chain: str):
    base = "https://api.etherscan.io/v2/api"
    params = {
        "chainid": _chainid_for(chain),
        "module": "proxy",
        "action": "eth_getBalance",
        "address": address,
        "tag": "latest",
        "apikey": ETHERSCAN_API_KEY
    }
    r = http_get(base, params=params, timeout=20)
    r.raise_for_status()
    return r.json()

def _etherscan_v2_proxy_eth_getTransactionCount(address: str, chain: str):
    base = "https://api.etherscan.io/v2/api"
    params = {
        "chainid": _chainid_for(chain),
        "module": "proxy",
        "action": "eth_getTransactionCount",
        "address": address,
        "tag": "latest",
        "apikey": ETHERSCAN_API_KEY
    }
    r = http_get(base, params=params, timeout=20)
    r.raise_for_status()
    return r.json()

def get_eth_address_stats(address: str, chain: str = "ethereum"):
    """
    Devuelve dict con:
      - balance_wei (int)
      - nonce (int)  -> número de txs salientes confirmadas
    """
    out = {"balance_wei": None, "nonce": None}
    try:
        bal = _etherscan_v2_proxy_eth_getBalance(address, chain)
        if isinstance(bal, dict) and isinstance(bal.get("result"), str):
            out["balance_wei"] = int(bal["result"], 16)
    except Exception:
        pass
    try:
        nc = _etherscan_v2_proxy_eth_getTransactionCount(address, chain)
        if isinstance(nc, dict) and isinstance(nc.get("result"), str):
            out["nonce"] = int(nc["result"], 16)
    except Exception:
        pass
    return out

# ---------------------------
# Parser ERC-20 transfers desde logs del receipt
# ---------------------------
def parse_erc20_transfers_from_receipt(receipt_json: dict):
    """
    Devuelve lista de dicts:
      [{from, to, value_raw, contract}]
    """
    TRANSFER_SIG = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    out = []
    try:
        logs = (receipt_json or {}).get("result", {}).get("logs", [])
        for lg in logs:
            topics = lg.get("topics", [])
            if topics and topics[0].lower() == TRANSFER_SIG:
                frm = "0x" + topics[1][-40:]
                to  = "0x" + topics[2][-40:]
                val = int(lg.get("data","0x0"), 16)
                out.append({
                    "from": frm,
                    "to": to,
                    "value_raw": val,
                    "contract": lg.get("address")
                })
    except Exception:
        pass
    return out

# ---------------------------
# TRON (TRX): TX + CUENTA (sin API key, TronScan)
# ---------------------------
def get_tron_tx(txid: str):
    """
    TronScan: info de transacción por hash (64 hex).
    """
    try:
        url = f"https://apilist.tronscanapi.com/api/transaction-info?hash={txid}"
        r = http_get(url, timeout=20)
        r.raise_for_status()
        data = r.json()
        if data and isinstance(data, dict) and data.get("hash"):
            return {
                "network": "tron",
                "tx_hash": data.get("hash"),
                "block": data.get("block"),
                "timestamp": data.get("timestamp"),
                "from": data.get("ownerAddress"),
                "to": data.get("toAddress"),
                "contract": data.get("contractType"),
                "confirmed": data.get("confirmed"),
                "fee": (data.get("cost") or {}).get("net_fee", 0),
                "raw": data,
            }
    except Exception:
        pass
    return None

def get_tron_account(address: str):
    """
    TronScan: info de cuenta (balance y tokens). Address en formato base58 (empieza por 'T').
    Retorna dict con 'balance_trx' (float aproximado) y 'trx' crudos si existen.
    """
    try:
        url = f"https://apilist.tronscanapi.com/api/account?address={address}"
        r = http_get(url, timeout=20)
        r.raise_for_status()
        data = r.json()
        out = {"balance_trx": None, "raw": data}
        # TronScan devuelve varios campos; priorizamos 'balance' (en SUN) si aparece.
        bal_sun = None
        if isinstance(data, dict):
            if "balance" in data:
                bal_sun = data.get("balance")
            elif "withPriceTokens" in data and isinstance(data["withPriceTokens"], list):
                # fallback: buscar TRX en withPriceTokens
                for t in data["withPriceTokens"]:
                    if t.get("tokenAbbr") == "TRX" and "balance" in t:
                        # algunos devuelven balance ya en TRX
                        try:
                            out["balance_trx"] = float(t.get("balance"))
                            return out
                        except Exception:
                            pass
        if bal_sun is not None:
            # 1 TRX = 1_000_000 SUN
            try:
                out["balance_trx"] = float(bal_sun) / 1_000_000.0
            except Exception:
                pass
        return out
    except Exception:
        return {"balance_trx": None, "raw": None}

