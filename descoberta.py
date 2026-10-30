"""Captura tudo que a tela Processos > Cliente recebe do servidor, para mapear os campos disponíveis.

Uso: python descoberta.py [indice_conta]   (0 = Greentax, 1 = Dvolv; padrão 0)
Saída: captura/<conta>_<data>/ com respostas JSON, frames de websocket, HTML, print e resumo_campos.txt
"""
import json
import os
import sys
import traceback
from datetime import datetime

from playwright.sync_api import sync_playwright

from config import CONTAS, HEADLESS, PASTA_CAPTURA, SLOW_MO, log
from navegacao import (
    abrir_processos_cliente, abrir_taskhub, aguardar_rede, fazer_login, fazer_logout,
    salvar_diagnostico, selecionar_filtro_prazo,
)

TIPOS_IGNORADOS = {"image", "font", "stylesheet", "media", "script"}


class Capturador:
    def __init__(self, pasta):
        self.pasta = pasta
        self.indice = []
        self.contador = 0
        self.etapa = "login"

    def _gravar(self, prefixo, conteudo, meta):
        self.contador += 1
        nome = f"{self.contador:04d}_{prefixo}.json"
        with open(os.path.join(self.pasta, nome), "w", encoding="utf-8") as f:
            json.dump(conteudo, f, ensure_ascii=False, indent=1)
        self.indice.append({"arquivo": nome, "etapa": self.etapa, **meta})

    def ao_receber_resposta(self, resposta):
        try:
            if resposta.request.resource_type in TIPOS_IGNORADOS:
                return
            tipo = resposta.headers.get("content-type", "")
            texto = resposta.text()
            try:
                corpo = json.loads(texto)
            except Exception:
                if "json" not in tipo and not texto.lstrip().startswith(("{", "[")):
                    return
                corpo = texto
            self._gravar("http", corpo, {
                "url": resposta.url, "metodo": resposta.request.method,
                "status": resposta.status, "tamanho": len(texto),
            })
        except Exception:
            pass

    def ao_abrir_websocket(self, ws):
        url = ws.url

        def ao_frame(dados):
            if isinstance(dados, bytes):
                return
            try:
                corpo = json.loads(dados)
            except Exception:
                corpo = dados
            self._gravar("ws", corpo, {"url": url, "tamanho": len(dados)})

        ws.on("framereceived", ao_frame)

    def salvar_indice(self):
        with open(os.path.join(self.pasta, "indice.json"), "w", encoding="utf-8") as f:
            json.dump(self.indice, f, ensure_ascii=False, indent=1)


def rolar_ate_o_fim(page, rodadas=40):
    altura_anterior = -1
    estavel = 0
    for _ in range(rodadas):
        altura = page.evaluate("""() => {
            let total = 0;
            for (const el of document.querySelectorAll('*')) {
                if (el.scrollHeight > el.clientHeight + 20 && getComputedStyle(el).overflowY.match(/auto|scroll/)) {
                    el.scrollTop = el.scrollHeight;
                    total += el.scrollHeight;
                }
            }
            window.scrollTo(0, document.body.scrollHeight);
            return total + document.body.scrollHeight;
        }""")
        page.wait_for_timeout(1000)
        estavel = estavel + 1 if altura == altura_anterior else 0
        if estavel >= 3:
            return
        altura_anterior = altura


def _caminhos(valor, prefixo="", saida=None, profundidade=0):
    """Achata o JSON em caminhos tipo 'data[].cliente.nome' -> exemplos de valor."""
    if saida is None:
        saida = {}
    if profundidade > 12:
        return saida
    if isinstance(valor, dict):
        chaves = list(valor.keys())
        if len(chaves) > 5 and all(isinstance(k, str) and k.startswith("-") for k in chaves[:5]):
            for v in list(valor.values())[:50]:
                _caminhos(v, prefixo + "{id}", saida, profundidade + 1)
        else:
            for k, v in valor.items():
                _caminhos(v, f"{prefixo}.{k}" if prefixo else k, saida, profundidade + 1)
    elif isinstance(valor, list):
        for v in valor[:50]:
            _caminhos(v, prefixo + "[]", saida, profundidade + 1)
    else:
        exemplos = saida.setdefault(prefixo, [])
        texto = str(valor)[:80]
        if texto not in exemplos and len(exemplos) < 3:
            exemplos.append(texto)
    return saida


def gerar_resumo(capturador):
    linhas = []
    for item in capturador.indice:
        if item["tamanho"] < 200:
            continue
        with open(os.path.join(capturador.pasta, item["arquivo"]), encoding="utf-8") as f:
            corpo = json.load(f)
        campos = _caminhos(corpo)
        if not campos:
            continue
        linhas.append(f"\n=== {item['arquivo']} [{item['etapa']}] {item.get('metodo', 'WS')} {item['url'][:150]} ({item['tamanho']} bytes)")
        for caminho, exemplos in sorted(campos.items())[:150]:
            linhas.append(f"  {caminho}: {' | '.join(exemplos)}")
    caminho = os.path.join(capturador.pasta, "resumo_campos.txt")
    with open(caminho, "w", encoding="utf-8") as f:
        f.write("\n".join(linhas))
    return caminho


def main(indice_conta=0):
    conta = CONTAS[indice_conta]
    pasta = os.path.join(PASTA_CAPTURA, f"{conta['subpasta']}_{datetime.now():%Y%m%d_%H%M%S}")
    os.makedirs(pasta, exist_ok=True)
    cap = Capturador(pasta)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS, slow_mo=SLOW_MO)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.set_default_timeout(30000)
        page.on("response", cap.ao_receber_resposta)
        page.on("websocket", cap.ao_abrir_websocket)
        try:
            fazer_login(page, conta)
            cap.etapa = "taskhub"
            abrir_taskhub(page)
            cap.etapa = "processos_cliente"
            abrir_processos_cliente(page)
            selecionar_filtro_prazo(page, "Todos")
            aguardar_rede(page)
            page.wait_for_timeout(3000)
            rolar_ate_o_fim(page)
            page.wait_for_timeout(2000)
            page.screenshot(path=os.path.join(pasta, "tela_cliente.png"), full_page=True)
            with open(os.path.join(pasta, "tela_cliente.html"), "w", encoding="utf-8") as f:
                f.write(page.content())
            cap.etapa = "logout"
            fazer_logout(page, conta)
        except Exception:
            log.error("Falha na descoberta:\n%s", traceback.format_exc())
            salvar_diagnostico(page, "falha_descoberta")
        finally:
            cap.salvar_indice()
            browser.close()

    resumo = gerar_resumo(cap)
    log.info("Capturados %d itens. Resumo dos campos: %s", len(cap.indice), resumo)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
