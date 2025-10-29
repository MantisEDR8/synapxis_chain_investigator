import os, time, json
from typing import Dict, List, Set
import requests

# ============================================================
# labels_auto.py — AUTO (gratis) con caché 24h + fallback
# ============================================================

_TTL_SECONDS = 24 * 3600  # 24h de caché
_cache_labels: Dict[str, Set[str]] = {}
_cache_time: float = 0.0

def _normalize_addrs(addrs: List[str]) -> Set[str]:
    out: Set[str] = set()
    for a in addrs or []:
        if not a:
            continue
        s = str(a).strip().strip('"').strip("'")
        if not s:
            continue
        out.add(s.lower())
    return out

# ---------- Semillas internas estables (por si las URLs fallan) ----------
def _seed_stables() -> Dict[str, Set[str]]:
    stable_eth = [
        # ETH mainnet (ERC-20)
        "0xdac17f958d2ee523a2206206994597c13d831ec7",  # USDT
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # USDC
        "0x6b175474e89094c44da98b954eedeac495271d0f",  # DAI
    ]
    stable_polygon = [
        # Polygon (ERC-20)
        "0xc2132d05d31c914a87c6611c10748aeb04b58e8f",  # USDT
        "0x2791bca1f2de4661ed88a30c99a7a9449aa84174",  # USDC (bridged)
        "0x8f3cf7ad23cd3cadbd9735aff958023239c6a063",  # DAI
    ]
    stable_tron = [
        # TRON (TRC-20)
        "tr7nhqjekqxgtci8q8zy4pl8otszgjlj6t",          # USDT TRC20
    ]
    return {
        "stable_contracts": _normalize_addrs(stable_eth + stable_polygon + stable_tron),
        "exchanges": set(),
        "mixers_bridges": set(),
        "ponzi_contracts": set(),
        "scam_addresses": set(),
    }

# ---------- Fuentes públicas (gratuitas) ----------
# Puedes ampliar/editar estas URLs (raw text o JSON). Si alguna cae, se ignora y sigue.
_DEFAULT_URLS = {
    "exchanges": [
        # Listas públicas de direcciones de exchanges (raw comunitarias)
        "https://raw.githubusercontent.com/bitcoin/bitcoin.org/master/exchanges.json",  # (puede variar formato)
    ],
    "mixers_bridges": [
        # Ejemplos comunitarios (algunos listan Tornado/Bridges)
        "https://raw.githubusercontent.com/OffcierCia/DeFi-Developer-Road-Map/main/src/resources/addresses/mixers.txt",
    ],
    "ponzi_contracts": [
        # Repos de investigación comunitaria (direcciones sospechosas)
        "https://raw.githubusercontent.com/MythXSecurity/known-bad-contracts/master/addresses.txt",
    ],
    "scam_addresses": [
        # Listas comunitarias (phishing/scam addresses)
        "https://raw.githubusercontent.com/WatchPug/scam-addresses/main/addresses.txt",
    ],
    "stable_contracts": [
        # Extra (además de semillas internas)
        "https://raw.githubusercontent.com/ethereum-lists/tokens/master/tokens/eth/0xdac17f958d2ee523a2206206994597c13d831ec7.json",
        "https://raw.githubusercontent.com/ethereum-lists/tokens/master/tokens/eth/0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48.json",
        "https://raw.githubusercontent.com/ethereum-lists/tokens/master/tokens/eth/0x6b175474e89094c44da98b954eedeac495271d0f.json",
    ],
}

def _env_urls_override() -> Dict[str, List[str]]:
    """
    Permite sobreescribir/añadir URLs via ENV:
    LABELS_URLS_JSON='{"scam_addresses":["https://.../scams.txt"]}'
    """
    try:
        raw = os.getenv("LABELS_URLS_JSON", "").strip()
        if not raw:
            return {}
        data = json.loads(raw)
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if isinstance(v, list)}
    except Exception:
        pass
    return {}

def _fetch_raw_list(url: str) -> List[str]:
    """
    Descarga texto/JSON y devuelve una lista de direcciones en minúscula.
    - TXT: líneas con direcciones (ignora comentarios y vacías)
    - JSON: intenta extraer 'address' o arrays simples de strings
    """
    try:
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        ct = r.headers.get("Content-Type", "").lower()
        text = r.text.strip()

        # JSON
        if "application/json" in ct or text.startswith("{") or text.startswith("["):
            try:
                data = json.loads(text)
                items = []
                if isinstance(data, list):
                    # lista simple
                    items = [str(x) for x in data if isinstance(x, (str, int))]
                elif isinstance(data, dict):
                    # intenta keys frecuentes
                    if "address" in data and isinstance(data["address"], str):
                        items = [data["address"]]
                    elif "addresses" in data and isinstance(data["addresses"], list):
                        items = [str(x) for x in data["addresses"] if isinstance(x, (str, int))]
                    else:
                        # barrido superficial de posibles arrays
                        for v in data.values():
                            if isinstance(v, list):
                                items += [str(x) for x in v if isinstance(x, (str, int))]
                return [s.lower() for s in items if s]
            except Exception:
                pass

        # TXT (línea por dirección)
        addrs = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or " " in line:
                continue
            addrs.append(line.lower())
        return addrs
    except Exception:
        return []

def _merge_labels(seeds: Dict[str, Set[str]], urls: Dict[str, List[str]]) -> Dict[str, Set[str]]:
    out = {k: set(v) for k, v in seeds.items()}
    for category, links in urls.items():
        for url in links:
            addrs = _fetch_raw_list(url)
            if not addrs:
                continue
            out.setdefault(category, set()).update(_normalize_addrs(addrs))
    return out

def get_labels(force_refresh: bool = False) -> Dict[str, Set[str]]:
    """
    Devuelve sets:
      { stable_contracts, exchanges, mixers_bridges, ponzi_contracts, scam_addresses }
    - Usa semillas internas (stables) + URLs públicas (si responden)
    - Caché 24h; configurable con LABELS_URLS_JSON
    """
    global _cache_labels, _cache_time
    now = time.time()
    if not force_refresh and _cache_labels and (now - _cache_time) < _TTL_SECONDS:
        return _cache_labels

    seeds = _seed_stables()
    urls_cfg = _DEFAULT_URLS.copy()

    # Permite añadir/override vía ENV (sin tocar el código)
    overrides = _env_urls_override()
    for k, v in overrides.items():
        urls_cfg.setdefault(k, [])
        urls_cfg[k].extend(v)

    labels = _merge_labels(seeds, urls_cfg)
    _cache_labels, _cache_time = labels, now
    return labels

