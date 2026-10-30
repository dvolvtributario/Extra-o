import json
import sys
import traceback
import urllib.request
from datetime import datetime

from playwright.sync_api import sync_playwright

import banco
from config import CONTAS, HEADLESS, SLOW_MO, log
from navegacao import abrir_taskhub, fazer_login, fazer_logout, salvar_diagnostico

URL_TASKHUB = "https://taskhubweb.firebaseio.com"
URL_PRINCIPAL = "https://emissaosimplesweb-default-rtdb.firebaseio.com"
MESES_A_FRENTE = 3

LER_SESSAO = """async () => new Promise((ok, erro) => {
    const req = indexedDB.open('firebaseLocalStorageDb');
    req.onerror = () => erro('IndexedDB indisponível');
    req.onsuccess = () => {
        const todos = req.result.transaction('firebaseLocalStorage', 'readonly')
            .objectStore('firebaseLocalStorage').getAll();
        todos.onsuccess = () => ok(todos.result.map(r => r.value).filter(v => v && v.stsTokenManager)
            .map(v => ({uid: v.uid, token: v.stsTokenManager.accessToken})));
    };
})"""


class ClienteFirebase:
    def __init__(self, uid, token):
        self.uid = uid
        self.token = token

    def ler(self, base, caminho, rasa=False, tentativas=3):
        url = f"{base}/{caminho}.json?auth={self.token}" + ("&shallow=true" if rasa else "")
        for tentativa in range(1, tentativas + 1):
            try:
                with urllib.request.urlopen(url, timeout=120) as resposta:
                    return json.loads(resposta.read()) or {}
            except urllib.error.HTTPError as erro:
                if erro.code in (401, 403):
                    raise PermissionError(f"Acesso negado a {caminho} (HTTP {erro.code}).")
                if tentativa == tentativas:
                    raise
            except Exception:
                if tentativa == tentativas:
                    raise
            log.info("Falha ao ler %s, tentando de novo (%d/%d).", caminho, tentativa, tentativas)


def obter_sessao(conta):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS, slow_mo=SLOW_MO)
        page = browser.new_page()
        page.set_default_timeout(30000)
        try:
            fazer_login(page, conta)
            abrir_taskhub(page)
            sessoes = page.evaluate(LER_SESSAO)
            if not sessoes:
                raise RuntimeError("Login feito, mas não achei o token da sessão no navegador.")
            fazer_logout(page, conta)
            return ClienteFirebase(sessoes[0]["uid"], sessoes[0]["token"])
        except Exception:
            salvar_diagnostico(page, f"falha_login_{conta['subpasta']}")
            raise
        finally:
            browser.close()


def _meses_para_ler(fb, no):
    limite = datetime.now().year * 12 + datetime.now().month - 1 + MESES_A_FRENTE
    meses = []
    for mes in fb.ler(URL_TASKHUB, f"data/{fb.uid}/{no}", rasa=True):
        if mes.isdigit() and len(mes) == 6 and int(mes[:4]) * 12 + int(mes[4:]) - 1 <= limite:
            meses.append(mes)
    return sorted(meses)


def _chaves(dic):
    return sorted(dic) if isinstance(dic, dict) else []


def _ms_para_iso(valor):
    if isinstance(valor, (int, float)):
        return datetime.fromtimestamp(valor / 1000).astimezone().isoformat()
    return valor


def ler_departamentos(fb):
    return [
        {"id": chave, "nome": (d.get("name") or chave).strip(), "gestor_id": d.get("manager")}
        for chave, d in fb.ler(URL_TASKHUB, f"data/{fb.uid}/teams").items() if isinstance(d, dict)
    ]


def ler_colaboradores(fb):
    perfis = fb.ler(URL_PRINCIPAL, f"users/{fb.uid}/profile/profiles")
    return [
        {"id": chave, "nome": (p.get("name") or "").strip(), "email": p.get("email"), "cargo": p.get("office") or p.get("role")}
        for chave, p in perfis.items() if isinstance(p, dict)
    ]


def ler_clientes(fb):
    clientes = []
    for chave, c in fb.ler(URL_PRINCIPAL, f"customers/{fb.uid}").items():
        if not isinstance(c, dict):
            continue
        status = c.get("status")
        tipo_doc = c.get("documentType")
        clientes.append({
            "id": chave,
            "nome": (c.get("name") or "").strip(),
            "documento": c.get("cpf") if tipo_doc == "cpf" else c.get("cnpj"),
            "regime": c.get("regime"),
            "cidade": c.get("city"),
            "uf": c.get("uf") or c.get("state"),
            "honorario": c.get("honorario"),
            "status": status.get("info") if isinstance(status, dict) else status,
            "ultimo_acesso_app": _ms_para_iso(c.get("dtCustomerLastSee")),
            "responsaveis": json.dumps(
                {o.get("teamId"): o.get("ownerId") for o in (c.get("owners") or {}).values() if isinstance(o, dict)}
                if isinstance(c.get("owners"), dict) else
                {o.get("teamId"): o.get("ownerId") for o in (c.get("owners") or []) if isinstance(o, dict)}
            ),
        })
    return clientes


def ler_processos(fb):
    processos = []
    for mes in _meses_para_ler(fb, "processes"):
        for chave, p in fb.ler(URL_TASKHUB, f"data/{fb.uid}/processes/{mes}").items():
            if not isinstance(p, dict):
                continue
            processos.append({
                "id": chave,
                "mes_prazo": mes,
                "cliente_id": p.get("customerId"),
                "titulo": (p.get("title") or "").strip(),
                "status": p.get("status"),
                "prazo": p.get("deadline"),
                "prazo_meta": p.get("goalDeadline"),
                "criado_em": p.get("creationDate"),
                "competencia": p.get("period"),
                "tem_multa": 1 if p.get("hasFine") else 0,
                "responsavel_id": p.get("ownerId"),
                "departamento_id": p.get("teamId"),
                "departamentos": json.dumps(_chaves(p.get("tasksTeams"))),
                "responsaveis_tarefas": json.dumps(_chaves(p.get("tasksOwners"))),
                "modelo_id": p.get("templateId"),
            })
        log.info("Processos de %s lidos.", mes)
    return processos


def ler_tarefas(fb):
    tarefas = []
    for mes in _meses_para_ler(fb, "tasks"):
        for processo_id, grupo in fb.ler(URL_TASKHUB, f"data/{fb.uid}/tasks/{mes}").items():
            if not isinstance(grupo, dict):
                continue
            for chave, t in grupo.items():
                if not isinstance(t, dict):
                    continue
                tarefas.append({
                    "id": chave,
                    "mes_prazo": mes,
                    "processo_id": t.get("processId") or processo_id,
                    "cliente_id": t.get("customerId"),
                    "titulo": (t.get("title") or "").strip(),
                    "status": t.get("status"),
                    "prazo": t.get("deadline"),
                    "prioridade": t.get("priority"),
                    "responsavel_id": t.get("ownerId"),
                    "departamento_id": t.get("teamId"),
                    "qtd_comentarios": len(t.get("comments") or {}),
                    "qtd_documentos": len(t.get("documents") or {}),
                })
        log.info("Tarefas de %s lidas.", mes)
    return tarefas


def ler_ordens_servico(fb):
    ordens = []
    for chave, o in fb.ler(URL_TASKHUB, f"data/{fb.uid}/requests").items():
        if not isinstance(o, dict):
            continue
        tipo = o.get("typeRequest") if isinstance(o.get("typeRequest"), dict) else {}
        ordens.append({
            "id": chave,
            "codigo": o.get("code"),
            "cliente_id": o.get("customerId"),
            "processo_id": o.get("processId"),
            "tipo": tipo.get("name"),
            "status": o.get("status"),
            "criado_em": _ms_para_iso(o.get("createdAt")),
            "responsaveis": json.dumps(_chaves(o.get("owners"))),
            "departamentos": json.dumps(_chaves(tipo.get("teams"))),
        })
    return ordens


TIPOS_LOG_GUARDADOS = {"status", "deadline", "goalDeadline", "owner"}


def ler_historico(fb):
    logs = []
    for tipo in ("task", "process"):
        for mes in sorted(fb.ler(URL_TASKHUB, f"userHistoric/{fb.uid}/{tipo}", rasa=True)):
            for chave, registro in (fb.ler(URL_TASKHUB, f"userHistoric/{fb.uid}/{tipo}/{mes}") or {}).items():
                if not isinstance(registro, dict) or registro.get("typeLog") not in TIPOS_LOG_GUARDADOS:
                    continue
                d = registro.get("data") if isinstance(registro.get("data"), dict) else {}
                logs.append({
                    "id": f"{tipo}:{chave}",
                    "tipo": "tarefa" if tipo == "task" else "processo",
                    "quando": registro.get("time"),
                    "acao": registro.get("typeAction"),
                    "tipo_log": registro.get("typeLog"),
                    "perfil_id": registro.get("profileId"),
                    "perfil_nome": (registro.get("profileName") or "").strip(),
                    "perfil_cargo": registro.get("profileOffice"),
                    "cliente_doc": d.get("customerDoc"),
                    "processo_titulo": d.get("processTitle"),
                    "tarefa_titulo": d.get("taskTitle"),
                    "valor_anterior": json.dumps(d.get("oldData"), ensure_ascii=False) if isinstance(d.get("oldData"), (dict, list)) else d.get("oldData"),
                    "valor_novo": json.dumps(d.get("newData"), ensure_ascii=False) if isinstance(d.get("newData"), (dict, list)) else d.get("newData"),
                })
        log.info("Histórico de %s lido.", tipo)
    return logs


def extrair_conta(indice_conta):
    conta = CONTAS[indice_conta]
    log.info("=== [%s] Iniciando extração ===", conta["nome_conta"])
    fb = obter_sessao(conta)
    dados = {
        "departamentos": ler_departamentos(fb),
        "colaboradores": ler_colaboradores(fb),
        "clientes": ler_clientes(fb),
        "processos": ler_processos(fb),
        "tarefas": ler_tarefas(fb),
        "ordens_servico": ler_ordens_servico(fb),
        "historico": ler_historico(fb),
    }
    execucao_id = banco.gravar_extracao(conta["subpasta"], conta["nome_conta"], dados)
    log.info(
        "=== [%s] Extração %d gravada: %s ===", conta["nome_conta"], execucao_id,
        ", ".join(f"{len(v)} {k}" for k, v in dados.items()),
    )
    return execucao_id


def main(indices):
    falhas = []
    for indice in indices:
        try:
            extrair_conta(indice)
        except Exception:
            log.error("[%s] Falha na extração:\n%s", CONTAS[indice]["nome_conta"], traceback.format_exc())
            falhas.append(CONTAS[indice]["nome_conta"])
    return falhas


if __name__ == "__main__":
    indices = [int(a) for a in sys.argv[1:]] or list(range(len(CONTAS)))
    sys.exit(1 if main(indices) else 0)
