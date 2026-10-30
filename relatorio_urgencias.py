import json
import os
import sys
from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

import pandas as pd
from playwright.sync_api import sync_playwright

import banco
from config import CONTAS, PASTA_PROJETO, log

FUSO = ZoneInfo("America/Sao_Paulo")
PASTA_RELATORIOS = os.environ.get("HUBSTROM_PASTA_RELATORIOS", os.path.join(PASTA_PROJETO, "relatorios"))
DIAS_PROXIMO_DA_MULTA = 7
DIAS_PROXIMOS = 7
STATUS_ABERTOS = ("pending", "inProgress")
NOME_STATUS = {"pending": "A fazer", "inProgress": "Em andamento"}

CATEGORIAS = {
    "em_multa": ("Em multa", "critico", "Processo com multa, vencido e não concluído"),
    "atrasado": ("Atrasados", "serio", "Vencido e não concluído (sem multa)"),
    "vence_hoje": ("Vencem hoje", "atencao", "Prazo hoje"),
    "proximo_multa": ("Próximo da multa", "atencao", f"Com multa, vence em até {DIAS_PROXIMO_DA_MULTA} dias"),
    "proximos": (f"Próximos {DIAS_PROXIMOS} dias", "neutro", f"Sem multa, vence em até {DIAS_PROXIMOS} dias"),
}


def _data_local(serie):
    return pd.to_datetime(serie, utc=True, errors="coerce", format="ISO8601").dt.tz_convert(FUSO).dt.date


ORDINAIS_DIA_UTIL = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "last": -1}


def _dia_util(ano, mes, posicao):
    """N-ésimo dia útil (seg–sex) do mês; posição -1 = último. Não considera feriados."""
    dias = [d for d in pd.date_range(f"{ano}-{mes:02d}-01", periods=31, freq="D")
            if d.month == mes and d.weekday() < 5]
    return dias[posicao if posicao < 0 else posicao - 1].date()


def prazo_das_tarefas(tarefas):
    """Data do prazo de cada tarefa. O Hubstrom guarda o prazo de três formas: data ISO,
    dia do mês ("15") ou dia útil ("fifth-working-day"); as duas últimas são relativas ao
    mês do prazo do processo (mes_prazo)."""
    datas = _data_local(tarefas["prazo"].where(tarefas["prazo"].astype(str).str.contains("T", na=False)))
    for i, prazo, mes_prazo in zip(tarefas.index, tarefas["prazo"], tarefas["mes_prazo"]):
        if pd.notna(datas.get(i)) or not prazo or not mes_prazo:
            continue
        ano, mes = int(str(mes_prazo)[:4]), int(str(mes_prazo)[4:6])
        texto = str(prazo)
        if texto.isdigit():
            ultimo = pd.Timestamp(year=ano, month=mes, day=1).days_in_month
            datas.at[i] = pd.Timestamp(year=ano, month=mes, day=min(int(texto), ultimo)).date()
        elif texto.endswith("-working-day") and texto.split("-")[0] in ORDINAIS_DIA_UTIL:
            datas.at[i] = _dia_util(ano, mes, ORDINAIS_DIA_UTIL[texto.split("-")[0]])
    return datas


def _lista(valor):
    try:
        return json.loads(valor) if valor else []
    except (TypeError, ValueError):
        return []


def carregar(extracao_id):
    with banco.conectar() as conexao:
        ler = lambda tabela: pd.read_sql(f"SELECT * FROM {tabela} WHERE extracao_id = ?", conexao, params=(extracao_id,))
        return {t: ler(t) for t in ("processos", "tarefas", "clientes", "colaboradores", "departamentos")}


def extracao_anterior(conta, antes_de):
    with banco.conectar() as conexao:
        linha = conexao.execute(
            "SELECT * FROM extracoes WHERE conta = ? AND substr(extraido_em, 1, 10) < ? ORDER BY id DESC LIMIT 1",
            (conta, antes_de.isoformat()),
        ).fetchone()
    return dict(linha) if linha else None


def classificar(processos, hoje):
    p = processos[processos["status"].isin(STATUS_ABERTOS)].copy()
    p["data_prazo"] = _data_local(p["prazo"])
    p["data_meta"] = _data_local(p["prazo_meta"])
    p = p[p["data_prazo"].notna()]
    p["dias"] = p["data_prazo"].map(lambda d: (d - hoje).days)
    multa = p["tem_multa"] == 1

    def categoria(linha, com_multa):
        if linha.dias < 0:
            return "em_multa" if com_multa else "atrasado"
        if linha.dias == 0:
            return "vence_hoje"
        if com_multa and linha.dias <= DIAS_PROXIMO_DA_MULTA:
            return "proximo_multa"
        if linha.dias <= DIAS_PROXIMOS:
            return "proximos"
        return None

    p["categoria"] = [categoria(l, m) for l, m in zip(p.itertuples(), multa)]
    p["meta_estourada"] = (p["dias"] >= 0) & p["data_meta"].notna() & (p["data_meta"] < hoje)
    return p


def montar_nomes(dados):
    clientes = dict(zip(dados["clientes"]["id"], dados["clientes"]["nome"]))
    pessoas = dict(zip(dados["colaboradores"]["id"], dados["colaboradores"]["nome"]))
    deptos = dict(zip(dados["departamentos"]["id"], dados["departamentos"]["nome"]))

    def cliente(cid):
        if not cid:
            return "(sem cliente)"
        return clientes.get(cid, "(cliente não encontrado)")

    def pessoa(pid):
        if not pid or pid == "no-owner":
            return "Sem responsável"
        return pessoas.get(pid, "(usuário excluído)")

    def responsavel_processo(linha):
        if linha.responsavel_id and linha.responsavel_id != "no-owner":
            return pessoa(linha.responsavel_id)
        nomes = [pessoa(i) for i in _lista(linha.responsaveis_tarefas) if i != "no-owner"]
        return ", ".join(dict.fromkeys(nomes)) or "Sem responsável"

    def departamento_processo(linha):
        ids = [linha.departamento_id] if linha.departamento_id else _lista(linha.departamentos)
        return ", ".join(dict.fromkeys(deptos.get(i, i) for i in ids)) or "—"

    return cliente, pessoa, responsavel_processo, departamento_processo, clientes


def analisar(dados, hoje):
    cliente, pessoa, resp_proc, depto_proc, mapa_clientes = montar_nomes(dados)
    processos = dados["processos"]
    de_cliente_ativo = processos["cliente_id"].isin(mapa_clientes)
    ignorados = int((~de_cliente_ativo & processos["status"].isin(STATUS_ABERTOS)).sum())
    p = classificar(processos[de_cliente_ativo], hoje)
    p["cliente"] = p["cliente_id"].map(cliente)
    p["responsavel"] = [resp_proc(l) for l in p.itertuples()]
    p["departamento"] = [depto_proc(l) for l in p.itertuples()]
    urgentes = p[p["categoria"].notna()]

    abertos = set(p["id"])
    t = dados["tarefas"]
    t = t[t["status"].isin(STATUS_ABERTOS) & t["processo_id"].isin(abertos)].copy()
    t["dias"] = prazo_das_tarefas(t).map(lambda d: (d - hoje).days if pd.notna(d) else None)
    t["responsavel"] = t["responsavel_id"].map(pessoa)
    tarefas_atrasadas = t[t["dias"] < 0]

    contagem = urgentes["categoria"].value_counts().to_dict()
    kpis = {c: int(contagem.get(c, 0)) for c in CATEGORIAS}
    kpis["meta_estourada"] = int(p["meta_estourada"].sum())
    kpis["tarefas_atrasadas"] = len(tarefas_atrasadas)

    linhas = []
    for l in urgentes.itertuples():
        for nome in l.responsavel.split(", "):
            linhas.append({"responsavel": nome, "categoria": l.categoria})
    por_resp = pd.DataFrame(linhas, columns=["responsavel", "categoria"])
    por_resp = pd.crosstab(por_resp["responsavel"], por_resp["categoria"]) if len(por_resp) else pd.DataFrame()
    por_resp = por_resp.reindex(columns=list(CATEGORIAS), fill_value=0)
    por_resp["tarefas_atrasadas"] = tarefas_atrasadas.groupby("responsavel").size()
    por_resp = por_resp.fillna(0).astype(int)
    por_resp = por_resp.sort_values(["em_multa", "atrasado", "vence_hoje", "tarefas_atrasadas"], ascending=False)

    por_cli = urgentes.groupby("cliente").agg(
        em_multa=("categoria", lambda s: (s == "em_multa").sum()),
        atrasado=("categoria", lambda s: (s == "atrasado").sum()),
        vence_hoje=("categoria", lambda s: (s == "vence_hoje").sum()),
        proximo_multa=("categoria", lambda s: (s == "proximo_multa").sum()),
        maior_atraso=("dias", lambda s: max(0, -s.min())),
        cliente_id=("cliente_id", "first"),
    )
    por_cli["pontos"] = por_cli["em_multa"] * 5 + por_cli["atrasado"] * 2 + por_cli["vence_hoje"] * 2 + por_cli["proximo_multa"]
    honorarios = dict(zip(dados["clientes"]["id"], dados["clientes"]["honorario"]))
    por_cli["honorario"] = por_cli["cliente_id"].map(honorarios)
    por_cli = por_cli.sort_values(["pontos", "maior_atraso"], ascending=False)

    vencidos = urgentes[urgentes["categoria"].isin(["em_multa", "atrasado"])]
    por_depto = vencidos.assign(departamento=vencidos["departamento"].str.split(", ")).explode("departamento")
    por_depto = por_depto.groupby("departamento").size().sort_values(ascending=False)

    return {
        "kpis": kpis,
        "urgentes": urgentes,
        "meta_estourada": p[p["meta_estourada"]],
        "por_responsavel": por_resp,
        "por_cliente": por_cli,
        "por_departamento": por_depto,
        "ignorados": ignorados,
    }


CSS = """
:root {
  --surface: #ffffff; --surface-2: #f6f6f4; --border: #e4e3df;
  --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #7a7974;
  --series-1: #2a78d6;
  --critico: #d03b3b; --serio: #ec835a; --atencao: #fab219; --bom: #0ca30c; --neutro: #9a9993;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--surface); color: var(--text-primary);
  font: 9.5pt/1.4 "Segoe UI", system-ui, sans-serif; }
h1 { font-size: 18pt; margin: 0; letter-spacing: -0.01em; }
h2 { font-size: 12pt; margin: 22px 0 8px; padding-bottom: 4px; border-bottom: 1px solid var(--border);
  break-after: avoid; display: flex; align-items: center; gap: 8px; }
h2 .qtd { font-weight: 400; color: var(--text-secondary); font-size: 10pt; }
.cabecalho { display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 14px; }
.cabecalho .sub { color: var(--text-secondary); margin-top: 2px; }
.cabecalho .extraido { color: var(--text-muted); font-size: 8.5pt; text-align: right; }
.kpis { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }
.kpi { background: var(--surface-2); border-radius: 6px; padding: 9px 11px; border-left: 4px solid var(--neutro); }
.kpi.critico { border-left-color: var(--critico); } .kpi.serio { border-left-color: var(--serio); }
.kpi.atencao { border-left-color: var(--atencao); }
.kpi .rotulo { color: var(--text-secondary); font-size: 8.5pt; }
.kpi .valor { font-size: 20pt; font-weight: 600; line-height: 1.15; font-variant-numeric: tabular-nums; }
.kpi .delta { color: var(--text-muted); font-size: 8pt; }
.destaques { margin-top: 12px; display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.destaque { border: 1px solid var(--border); border-radius: 6px; padding: 9px 11px; }
.destaque h3 { margin: 0 0 4px; font-size: 9.5pt; }
.destaque ol { margin: 0; padding-left: 18px; }
.destaque li { margin: 1px 0; }
.destaque .num { color: var(--text-secondary); }
table { width: 100%; border-collapse: collapse; font-size: 8.5pt; }
thead { display: table-header-group; }
th { text-align: left; font-weight: 600; color: var(--text-secondary); padding: 4px 6px;
  border-bottom: 1px solid var(--border); white-space: nowrap; }
td { padding: 4px 6px; border-bottom: 1px solid var(--surface-2); vertical-align: top; }
tr { break-inside: avoid; }
td.n, th.n { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
td.zero { color: var(--text-muted); }
.selo { display: inline-flex; align-items: center; gap: 4px; white-space: nowrap; font-size: 8pt; }
.selo::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: var(--neutro); }
.selo.critico::before { background: var(--critico); } .selo.serio::before { background: var(--serio); }
.selo.atencao::before { background: var(--atencao); }
.barra { display: flex; align-items: center; gap: 6px; }
.barra span.trilho { flex: 1; height: 8px; }
.barra span.fill { display: block; height: 8px; background: var(--series-1); border-radius: 0 4px 4px 0; min-width: 2px; }
.barra span.v { width: 28px; text-align: right; font-variant-numeric: tabular-nums; }
.vazio { color: var(--text-muted); font-style: italic; padding: 6px 0; }
.nota { color: var(--text-muted); font-size: 8pt; margin-top: 22px; border-top: 1px solid var(--border); padding-top: 8px; }
.nota dt { font-weight: 600; display: inline; } .nota dd { display: inline; margin: 0 10px 0 2px; }
"""


def _fmt_data(d):
    return d.strftime("%d/%m/%Y") if pd.notna(d) else "—"


def _fmt_dias(dias):
    if dias < 0:
        return f"há {-dias} dia{'s' if dias < -1 else ''}"
    if dias == 0:
        return "hoje"
    return f"em {dias} dia{'s' if dias > 1 else ''}"


def _numero(valor):
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return None
    if isinstance(valor, str):
        texto = valor.replace("R$", "").strip()
        if "," in texto:
            texto = texto.replace(".", "").replace(",", ".")
        try:
            return float(texto) if texto else None
        except ValueError:
            return None
    return float(valor)


def _fmt_moeda(valor):
    numero = _numero(valor)
    if not numero:
        return "—"
    return "R$ " + f"{numero / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _celula_n(valor):
    return f'<td class="n{" zero" if not valor else ""}">{valor}</td>'


def tabela_processos(df, ordem, ascendente, mostrar_selo=False):
    if df.empty:
        return '<p class="vazio">Nenhum processo nesta situação.</p>'
    df = df.sort_values(ordem, ascending=ascendente)
    linhas = []
    for l in df.itertuples():
        selo = ""
        if mostrar_selo:
            nome, classe, _ = CATEGORIAS[l.categoria]
            selo = f'<td><span class="selo {classe}">{nome}</span></td>'
        linhas.append(
            f"<tr><td>{escape(l.cliente)}</td><td>{escape(l.titulo)}</td><td>{escape(l.departamento)}</td>"
            f"<td>{escape(l.responsavel)}</td><td class='n'>{_fmt_data(l.data_prazo)}</td>"
            f"<td class='n'>{_fmt_dias(l.dias)}</td><td>{NOME_STATUS.get(l.status, l.status)}</td>{selo}</tr>"
        )
    cab_selo = "<th>Situação</th>" if mostrar_selo else ""
    return (
        "<table><thead><tr><th>Cliente</th><th>Processo</th><th>Departamento</th><th>Responsável</th>"
        f"<th class='n'>Prazo</th><th class='n'>Quando</th><th>Status</th>{cab_selo}</tr></thead>"
        f"<tbody>{''.join(linhas)}</tbody></table>"
    )


def secao(titulo, classe, qtd, corpo):
    selo = f'<span class="selo {classe}"></span>' if classe else ""
    return f'<h2>{selo}{escape(titulo)} <span class="qtd">· {qtd}</span></h2>{corpo}'


def gerar_html(conta, extracao, analise, analise_anterior, hoje):
    k = analise["kpis"]
    urg = analise["urgentes"]
    ref_ant = analise_anterior["data"] if analise_anterior else None

    def delta(chave):
        if not analise_anterior:
            return ""
        dif = k[chave] - analise_anterior["kpis"][chave]
        sinal = "+" if dif > 0 else ""
        return f'<div class="delta">{sinal}{dif} vs. {ref_ant:%d/%m}</div>' if dif else f'<div class="delta">= {ref_ant:%d/%m}</div>'

    cards = [
        ("em_multa", "Em multa", "critico"), ("atrasado", "Atrasados", "serio"),
        ("vence_hoje", "Vencem hoje", "atencao"), ("proximo_multa", f"Próximo da multa ({DIAS_PROXIMO_DA_MULTA}d)", "atencao"),
        ("proximos", f"Vencem em {DIAS_PROXIMOS} dias", ""), ("meta_estourada", "Meta interna estourada", ""),
        ("tarefas_atrasadas", "Tarefas atrasadas", ""),
    ]
    kpis_html = "".join(
        f'<div class="kpi {c}"><div class="rotulo">{r}</div><div class="valor">{k[ch]}</div>{delta(ch)}</div>'
        for ch, r, c in cards
    )

    resp = analise["por_responsavel"]
    top_resp = "".join(
        f"<li>{escape(nome)} <span class='num'>— {l.em_multa} em multa, {l.atrasado} atrasados, {l.vence_hoje} hoje</span></li>"
        for nome, l in resp.head(5).iterrows()
    ) or "<li>Ninguém com pendências urgentes.</li>"
    cli = analise["por_cliente"]
    top_cli = "".join(
        f"<li>{escape(nome)} <span class='num'>— {l.em_multa} em multa, {l.atrasado} atrasados, {l.vence_hoje} hoje</span></li>"
        for nome, l in cli.head(5).iterrows()
    ) or "<li>Nenhum cliente com pendências urgentes.</li>"

    linhas_resp = "".join(
        f"<tr><td>{escape(nome)}</td>{_celula_n(l.em_multa)}{_celula_n(l.atrasado)}{_celula_n(l.vence_hoje)}"
        f"{_celula_n(l.proximo_multa)}{_celula_n(l.proximos)}{_celula_n(l.tarefas_atrasadas)}</tr>"
        for nome, l in resp.iterrows()
    )
    tabela_resp = (
        "<table><thead><tr><th>Responsável</th><th class='n'>Em multa</th><th class='n'>Atrasados</th>"
        "<th class='n'>Vencem hoje</th><th class='n'>Próx. multa</th><th class='n'>Próx. 7 dias</th>"
        f"<th class='n'>Tarefas atrasadas</th></tr></thead><tbody>{linhas_resp}</tbody></table>"
    ) if len(resp) else '<p class="vazio">Sem pendências urgentes.</p>'

    linhas_cli = "".join(
        f"<tr><td>{escape(nome)}</td>{_celula_n(l.em_multa)}{_celula_n(l.atrasado)}{_celula_n(l.vence_hoje)}"
        f"{_celula_n(l.proximo_multa)}<td class='n'>{l.maior_atraso or '—'}</td><td class='n'>{_fmt_moeda(l.honorario)}</td></tr>"
        for nome, l in cli.head(20).iterrows()
    )
    tabela_cli = (
        "<table><thead><tr><th>Cliente</th><th class='n'>Em multa</th><th class='n'>Atrasados</th>"
        "<th class='n'>Vencem hoje</th><th class='n'>Próx. multa</th><th class='n'>Maior atraso (dias)</th>"
        f"<th class='n'>Honorário</th></tr></thead><tbody>{linhas_cli}</tbody></table>"
    ) if len(cli) else '<p class="vazio">Nenhum cliente com pendências urgentes.</p>'

    depto = analise["por_departamento"]
    maximo = depto.max() if len(depto) else 1
    linhas_depto = "".join(
        f"<tr><td style='width:32%'>{escape(nome)}</td><td><div class='barra'><span class='trilho'>"
        f"<span class='fill' style='width:{v / maximo * 100:.1f}%'></span></span><span class='v'>{v}</span></div></td></tr>"
        for nome, v in depto.items()
    )
    tabela_depto = (
        f"<table><thead><tr><th>Departamento</th><th>Processos vencidos (atrasados + em multa)</th></tr></thead>"
        f"<tbody>{linhas_depto}</tbody></table>"
    ) if len(depto) else '<p class="vazio">Nenhum processo vencido.</p>'

    def cat(c):
        return urg[urg["categoria"] == c]

    corpo = [
        secao("Em multa", "critico", k["em_multa"], tabela_processos(cat("em_multa"), "dias", True)),
        secao("Vencem hoje", "atencao", k["vence_hoje"], tabela_processos(cat("vence_hoje"), ["tem_multa", "cliente"], [False, True])),
        secao(f"Próximo da multa (até {DIAS_PROXIMO_DA_MULTA} dias)", "atencao", k["proximo_multa"], tabela_processos(cat("proximo_multa"), "dias", True)),
        secao("Por responsável", "", len(resp), tabela_resp),
        secao("Clientes com mais urgências", "", len(cli), tabela_cli),
        secao("Por departamento", "", len(depto), tabela_depto),
        secao("Atrasados (sem multa)", "serio", k["atrasado"], tabela_processos(cat("atrasado"), "dias", False)),
        secao(f"Vencem nos próximos {DIAS_PROXIMOS} dias (sem multa)", "", k["proximos"], tabela_processos(cat("proximos"), "dias", True)),
        secao("Meta interna estourada (ainda no prazo legal)", "", k["meta_estourada"],
              tabela_processos(analise["meta_estourada"].assign(
                  cliente=analise["meta_estourada"]["cliente"]), "dias", True)),
    ]

    extraido = datetime.fromisoformat(extracao["extraido_em"]).astimezone(FUSO)
    notas = "".join(f"<dt>{n}:</dt><dd>{escape(d)}.</dd>" for n, _, d in CATEGORIAS.values())
    return f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<title>Urgências do dia — {escape(conta['nome_conta'])}</title><style>{CSS}</style></head><body>
<div class="cabecalho"><div><h1>Urgências do dia</h1>
<div class="sub">{escape(conta['nome_conta'])} · {hoje:%d/%m/%Y}</div></div>
<div class="extraido">Dados extraídos do Hubstrom em {extraido:%d/%m/%Y às %H:%M}</div></div>
<div class="kpis">{kpis_html}</div>
<div class="destaques">
<div class="destaque"><h3>Quem precisa agir primeiro</h3><ol>{top_resp}</ol></div>
<div class="destaque"><h3>Clientes que precisam de atenção</h3><ol>{top_cli}</ol></div>
</div>
{''.join(corpo)}
<dl class="nota">{notas}<dt>Meta interna estourada:</dt><dd>passou do prazo meta do processo, mas ainda está dentro do prazo legal.</dd>
<dt>Tarefas atrasadas:</dt><dd>tarefas abertas, de processos abertos, com prazo vencido.</dd>
Considera apenas processos com status "A fazer" ou "Em andamento", de clientes ativos
({analise['ignorados']} processos abertos de clientes excluídos foram ignorados, como o Hubstrom faz).
Honorário: valor cadastrado no Hubstrom; "—" quando não informado.</dl>
</body></html>"""


RODAPE = """<div style="font: 7pt 'Segoe UI', sans-serif; color: #7a7974; width: 100%; padding: 0 12mm;
display: flex; justify-content: space-between;"><span>{titulo}</span>
<span>Página <span class="pageNumber"></span> de <span class="totalPages"></span></span></div>"""


def gerar_pdf(htmls):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        for caminho, html, titulo in htmls:
            page.set_content(html, wait_until="load")
            page.pdf(
                path=caminho, format="A4", print_background=True,
                margin={"top": "12mm", "bottom": "14mm", "left": "12mm", "right": "12mm"},
                display_header_footer=True, header_template="<span></span>",
                footer_template=RODAPE.format(titulo=escape(titulo)),
            )
            log.info("Relatório gerado: %s", caminho)
        browser.close()


def gerar(indices):
    hoje = datetime.now(FUSO).date()
    pasta = os.path.join(PASTA_RELATORIOS, hoje.isoformat())
    os.makedirs(pasta, exist_ok=True)
    saidas = []
    for indice in indices:
        conta = CONTAS[indice]
        extracao = banco.ultima_extracao(conta["subpasta"])
        if not extracao:
            log.error("[%s] Nenhuma extração no banco; rode o extrator primeiro.", conta["nome_conta"])
            continue
        analise = analisar(carregar(extracao["id"]), hoje)
        anterior = extracao_anterior(conta["subpasta"], hoje)
        analise_anterior = None
        if anterior:
            data_ant = datetime.fromisoformat(anterior["extraido_em"]).astimezone(FUSO).date()
            analise_anterior = analisar(carregar(anterior["id"]), data_ant)
            analise_anterior["data"] = data_ant
        html = gerar_html(conta, extracao, analise, analise_anterior, hoje)
        caminho = os.path.join(pasta, f"Urgencias_{conta['subpasta']}_{hoje.isoformat()}.pdf")
        with open(caminho[:-4] + ".html", "w", encoding="utf-8") as f:
            f.write(html)
        saidas.append((caminho, html, f"Urgências do dia · {conta['nome_conta']} · {hoje:%d/%m/%Y}"))
    gerar_pdf(saidas)
    return [s[0] for s in saidas]


if __name__ == "__main__":
    argumentos = sys.argv[1:]
    indices = [int(a) for a in argumentos if a.isdigit()] or list(range(len(CONTAS)))
    if "--extrair" in argumentos:
        import extrator
        extrator.main(indices)
    gerar(indices)
