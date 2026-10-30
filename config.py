import os
import logging
import sys

PASTA_PROJETO = os.path.dirname(os.path.abspath(__file__))


def _carregar_env(caminho):
    if not os.path.exists(caminho):
        return
    with open(caminho, encoding="utf-8") as f:
        for linha in f:
            linha = linha.strip()
            if not linha or linha.startswith("#") or "=" not in linha:
                continue
            chave, valor = linha.split("=", 1)
            os.environ.setdefault(chave.strip(), valor.strip().strip('"').strip("'"))


_carregar_env(os.path.join(PASTA_PROJETO, ".env"))

HEADLESS = os.environ.get("HUBSTROM_HEADLESS", "1") != "0"
SLOW_MO = 0 if HEADLESS else 300

PASTA_CAPTURA = os.path.join(PASTA_PROJETO, "captura")
PASTA_DIAGNOSTICO = os.path.join(PASTA_PROJETO, "diagnostico")
os.makedirs(PASTA_CAPTURA, exist_ok=True)
os.makedirs(PASTA_DIAGNOSTICO, exist_ok=True)

CONTAS = [
    {
        "nome_conta": "Empresa 1 (Greentax)",
        "email": os.environ.get("HUBSTROM_EMAIL_1"),
        "senha": os.environ.get("HUBSTROM_SENHA_1"),
        "perfil_botao": os.environ.get("HUBSTROM_PERFIL_1"),
        "pin": os.environ.get("HUBSTROM_PIN_1"),
        "subpasta": "Empresa1",
    },
    {
        "nome_conta": "Empresa 2 (Dvolv)",
        "email": os.environ.get("HUBSTROM_EMAIL_2"),
        "senha": os.environ.get("HUBSTROM_SENHA_2"),
        "perfil_botao": os.environ.get("HUBSTROM_PERFIL_2"),
        "pin": os.environ.get("HUBSTROM_PIN_2"),
        "subpasta": "Empresa2",
    },
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(PASTA_PROJETO, "hubstrom_tarefas_log.txt"), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("hubstrom_tarefas")
