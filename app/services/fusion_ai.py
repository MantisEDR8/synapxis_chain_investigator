"""
Synapxis FusionAI — Módulo de enriquecimiento inteligente de datos on-chain.
Si las APIs no devuelven información suficiente, FusionAI analiza el contexto
y genera una interpretación técnica complementaria para el informe final.
"""

import os

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

try:
    from openai import OpenAI
    _OPENAI_AVAILABLE = True
except ImportError:
    OpenAI = None
    _OPENAI_AVAILABLE = False


def enrich_with_ai(meta: dict, events: list, transfers: list) -> str:
    """
    Genera un análisis interpretativo con IA a partir de los datos disponibles.
    Si falta la librería o la API key, devuelve un texto neutro y no rompe nada.
    """

    if not _OPENAI_AVAILABLE or not OPENAI_API_KEY:
        return "[FusionAI no disponible] Falta librería 'openai' o variable OPENAI_API_KEY."

    resumen = {
        "network": meta.get("network", "N/D"),
        "from": meta.get("from", "N/D"),
        "to": meta.get("to", "N/D"),
        "risk_band": meta.get("risk_band", "N/D"),
        "risk_score": meta.get("risk_score", "N/D"),
        "transfers_detected": len(transfers) if transfers else 0,
        "events_detected": len(events) if events else 0
    }

    prompt = f"""
    Eres Synapxis FusionAI, un analista blockchain experto.
    Analiza el siguiente conjunto de datos de una wallet o transacción:

    Metadata:
    {resumen}

    Eventos detectados:
    {events[:5] if events else "Ninguno"}

    Transferencias detectadas:
    {transfers[:5] if transfers else "Ninguna"}

    Debes elaborar un resumen técnico conciso que incluya:
    - Tipo de operación probable (movimiento interno, swap, ingreso, drenaje, etc.)
    - Riesgo percibido (bajo, medio, alto) según los datos.
    - Posible relación entre las direcciones origen/destino.
    - Observaciones relevantes sobre la actividad general.

    Responde en lenguaje profesional, en español, con frases cortas.
    """

    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Eres un analista blockchain técnico de Synapxis."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.4,
            max_tokens=350
        )

        analysis = response.choices[0].message.content.strip()
        return analysis

    except Exception as e:
        return f"[FusionAI no disponible] Error: {str(e)}"
