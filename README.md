# Synapxis Chain Investigator (Local Dashboard — Free APIs Only)

Dashboard local para pegar una wallet o tx hash y generar tu informe **Informe_Wallet_Synapxis** en DOCX/PDF/CSV.
Usa **solo APIs gratuitas** (Etherscan, Polygonscan, Covalent, CoinGecko).

## Instalación rápida
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
mkdir -p outputs assets
# añade tu logo en assets/logo.jpeg (opcional)
```
## Ejecutar
```bash
./start-board.sh
# abre http://localhost:8000
```
## CLI (opcional)
```bash
python cli.py --tx 0x...      # o --address 0x...
```
## Notas
- PDF requiere LibreOffice (comando `soffice` en PATH). Si no está, tendrás DOCX y CSV.
- CSV se genera automáticamente si hay datos útiles.
