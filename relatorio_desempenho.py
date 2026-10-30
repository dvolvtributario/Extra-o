import os
import sys
from datetime import datetime
from html import escape

import pandas as pd

import banco
import historico
from config import CONTAS, log
import graficos as gf
from relatorio_urgencias import CSS, FUSO, PASTA_RELATORIOS, _celula_n, _data_local, _lista, gerar_pdf, prazo_das_tarefas

MESES_TENDENCIA = 6
MIN_TAREFAS_COMPARACAO = 3
NOMES_MES = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]


def _mes_rotulo(aaaamm):
    return f"{NOMES_MES[int(aaaamm[4:]) - 1]}/{aaaamm[:4]}"


def _mes_anterior(aaaamm, n=1):
    ano, mes = int(aaaamm[:4]), int(aaaamm[4:])
    total = ano * 12 + mes - 1 - n
    return f"{total // 12}{total % 12 + 1:02d}"


def _pct(parte, total):
    return round(parte / total * 100) if total else None


def _fmt_pct(valor):
    return "—" if valor is None else f"{valor}%"


def _fmt_dias(valor):
    return "—" if valor is None or pd.isna(valor) else f"{valor:.1f}".replace(".", ",")


def carregar(extracao_id):
    with banco.conectar() as conexao:
        ler = lambda tabela: pd.read_sql(f"SELECT * FROM {tabela} WHERE extracao_id = ?", conexao, params=(extracao_id,))
        return {t: ler(t) for t in ("processos", "tarefas", "clientes", "colaboradores", "departamentos", "historico")}


def preparar(dados):
    clientes_ativos = set(dados["clientes"]["id"])
    processos = dados["processos"][dados["processos"]["cliente_id"].isin(clientes_ativos)]
    tarefas = dados["tarefas"][dados["tarefas"]["processo_id"].isin(set(processos["id"]))]
    deptos = dict(zip(dados["departamentos"]["id"], dados["departamentos"]["nome"]))
    pessoas = dict(zip(dados["colaboradores"]["id"], dados["colaboradores"]["nome"]))

    logs = historico.ligar_logs_de_tarefas(dados["historico"], tarefas, processos, dados["clientes"])
    conc = historico.conclusoes_de_tarefas(logs, tarefas)
    conc["data_conclusao"] = conc["concluida_em"].dt.tz_convert(FUSO).dt.date
    conc["data_prazo"] = prazo_das_tarefas(conc)
    conc = conc[conc["data_prazo"].notna()]
    conc["dif"] = [(c - p).days for c, p in zip(conc["data_conclusao"], conc["data_prazo"])]
    conc["no_prazo"] = conc["dif"] <= 0
    conc["mes"] = conc["data_conclusao"].map(lambda d: f"{d.year}{d.month:02d}")
    conc["departamento"] = conc["departamento_id"].map(lambda i: deptos.get(i, i) if i else "—")
    conc["responsavel"] = conc["responsavel_id"].map(lambda i: pessoas.get(i, "Sem responsável"))
    titulo_proc = dict(zip(processos["id"], processos["titulo"]))
    conc["tipo"] = conc["processo_id"].map(titulo_proc).fillna("—")

    status = dados["historico"][(dados["historico"]["tipo"] == "tarefa") & (dados["historico"]["tipo_log"] == "status")].copy()
    status["data"] = _data_local(status["quando"])
    status["mes"] = status["data"].map(lambda d: f"{d.year}{d.month:02d}" if pd.notna(d) else None)

    concluidos = processos[processos["status"] == "completed"]
    por_proc = conc.groupby("processo_id")["concluida_em"].agg(["max", "count"])
    qtd_concluidas = tarefas[tarefas["status"] == "completed"].groupby("processo_id").size()
    proc = concluidos.set_index("id").join(por_proc, how="inner")
    proc = proc[proc["count"] == qtd_concluidas.reindex(proc.index).fillna(0)]
    proc["data_conclusao"] = proc["max"].dt.tz_convert(FUSO).dt.date
    proc["data_prazo"] = _data_local(proc["prazo"])
    proc["data_meta"] = _data_local(proc["prazo_meta"])
    proc["no_prazo"] = proc["data_conclusao"] <= proc["data_prazo"]
    proc["na_meta"] = [(c <= m) if pd.notna(m) else None for c, m in zip(proc["data_conclusao"], proc["data_meta"])]
    proc["mes"] = proc["data_conclusao"].map(lambda d: f"{d.year}{d.month:02d}")

    def depto_processo(linha):
        ids = [linha.departamento_id] if linha.departamento_id else _lista(linha.departamentos)
        return deptos.get(ids[0], ids[0]) if ids else "—"

    proc["departamento"] = [depto_processo(l) for l in proc.itertuples()]

    concluidas = tarefas[tarefas["status"] == "completed"]
    com_data = concluidas["id"].isin(set(conc["id"]))
    cobertura = {
        "logs_ligados": _pct(int(logs["tarefa_id"].notna().sum()), len(logs)),
        "por_mes_prazo": {m: (_pct(int(g.sum()), len(g)), len(g)) for m, g in com_data.groupby(concluidas["mes_prazo"])},
        "inicio_historico": status["data"].min(),
    }
    gestores = {deptos.get(i, i): pessoas.get(g) for i, g in zip(dados["departamentos"]["id"], dados["departamentos"]["gestor_id"]) if g}
    return conc, status, proc, cobertura, gestores


def indicadores(conc, status, proc, mes):
    c = conc[conc["mes"] == mes]
    s = status[status["mes"] == mes]
    p = proc[proc["mes"] == mes]
    atrasadas = c[~c["no_prazo"]]
    metas = p["na_meta"].dropna()
    return {
        "concluidas": len(c),
        "pct_no_prazo": _pct(int(c["no_prazo"].sum()), len(c)),
        "atraso_medio": atrasadas["dif"].mean() if len(atrasadas) else None,
        "dispensadas": int((s["valor_novo"] == "dismissed").sum()),
        "reabertas": int(((s["valor_anterior"] == "completed") & s["valor_novo"].isin(["pending", "inProgress"])).sum()),
        "processos": len(p),
        "pct_proc_prazo": _pct(int(p["no_prazo"].sum()), len(p)),
        "pct_proc_meta": _pct(int(metas.astype(bool).sum()), len(metas)),
    }


def resumo_grupo(df, coluna):
    g = df.groupby(coluna).agg(
        concluidas=("id", "size"),
        no_prazo=("no_prazo", "sum"),
        atraso_medio=("dif", lambda s: s[s > 0].mean() if (s > 0).any() else None),
    )
    g["pct"] = [round(n / t * 100) for n, t in zip(g["no_prazo"], g["concluidas"])]
    g["atrasadas"] = g["concluidas"] - g["no_prazo"]
    return g.sort_values("concluidas", ascending=False).astype(object)


FAIXAS_ATRASO = [("No prazo", None, 0), ("1–3 d", 1, 3), ("4–7 d", 4, 7), ("8–15 d", 8, 15), ("16–30 d", 16, 30), ("+30 d", 31, None)]
MIN_VOLUME = 10


def _moda(serie):
    serie = serie.dropna()
    return serie.mode().iloc[0] if len(serie) else None


def _lista_texto(itens):
    itens = list(itens)
    return itens[0] if len(itens) == 1 else ", ".join(itens[:-1]) + " e " + itens[-1]


def diagnosticar(conc, status, proc, mes, gestores):
    """Regras simples sobre os dados do mês -> frases de 'Por quê?' por seção e ações sugeridas.
    São indícios dos dados, não certezas: o texto é escrito nesse tom."""
    k = indicadores(conc, status, proc, mes)
    ka = indicadores(conc, status, proc, _mes_anterior(mes))
    c = conc[conc["mes"] == mes]
    s = status[status["mes"] == mes]
    atrasadas = c[~c["no_prazo"]]
    porque = {"evolucao": [], "atrasos": [], "pessoas": [], "departamentos": []}
    acoes = []
    resumo = []

    def gestor_de(depto):
        return gestores.get(depto) or f"Gestor(a) de {depto}"

    if not len(c):
        return porque, acoes, ["Nenhuma tarefa concluída com data identificada no período."]

    if ka["pct_no_prazo"] is not None:
        dif = k["pct_no_prazo"] - ka["pct_no_prazo"]
        if abs(dif) >= 10:
            sentido = "caiu" if dif < 0 else "subiu"
            porque["evolucao"].append(
                f"A pontualidade das tarefas {sentido} de {ka['pct_no_prazo']}% para {k['pct_no_prazo']}% "
                f"({'+' if dif > 0 else ''}{dif} p.p.) em relação a {_mes_rotulo(_mes_anterior(mes))}.")
            resumo.append(f"Pontualidade das tarefas {sentido} {abs(dif)} p.p.: {k['pct_no_prazo']}% concluídas no prazo.")
        if ka["concluidas"]:
            var = round((k["concluidas"] - ka["concluidas"]) / ka["concluidas"] * 100)
            if abs(var) >= 15:
                porque["evolucao"].append(
                    f"O volume de tarefas concluídas {'aumentou' if var > 0 else 'diminuiu'} {abs(var)}% "
                    f"({ka['concluidas']} → {k['concluidas']}).")
    if k["pct_proc_prazo"] is not None and k["pct_proc_prazo"] - k["pct_no_prazo"] >= 20:
        porque["evolucao"].append(
            f"{k['pct_proc_prazo']}% dos processos foram entregues no prazo legal, mas só {k['pct_no_prazo']}% das tarefas "
            "cumpriram o próprio prazo. Isso indica que os prazos das tarefas nos modelos estão mais apertados do que a "
            "rotina real: o atraso da tarefa nem sempre vira atraso para o cliente.")
        acoes.append({
            "titulo": "Revisar os prazos das tarefas nos modelos de processo",
            "porque": f"Diferença de {k['pct_proc_prazo'] - k['pct_no_prazo']} p.p. entre processos no prazo legal "
                      f"({k['pct_proc_prazo']}%) e tarefas no prazo ({k['pct_no_prazo']}%).",
            "responsavel": "Gestão do TaskHub (modelos)",
            "acompanhar": "% de tarefas no prazo nos próximos meses",
            "peso": len(atrasadas),
        })

    if len(atrasadas):
        longos = int((atrasadas["dif"] > 15).sum())
        pct_longos = _pct(longos, len(atrasadas))
        if pct_longos >= 40:
            porque["atrasos"].append(
                f"{pct_longos}% das tarefas atrasadas foram concluídas mais de 15 dias depois do prazo. Atrasos longos "
                "costumam indicar tarefas esquecidas abertas, dependência do cliente ou prazo de modelo irreal, "
                "mais do que falta de ritmo.")
        else:
            porque["atrasos"].append(
                f"A maior parte dos atrasos é curta: {100 - pct_longos}% foram concluídas até 15 dias após o prazo.")
        por_tipo = atrasadas.groupby("tipo").agg(qtd=("id", "size"), media=("dif", "mean"), depto=("departamento", _moda))
        por_tipo = por_tipo.sort_values("qtd", ascending=False)
        top = por_tipo.head(3)
        pct_top = _pct(int(top["qtd"].sum()), len(atrasadas))
        porque["atrasos"].append(
            f"Os atrasos são concentrados: {_lista_texto([f'“{t}”' for t in top.index])} somam {pct_top}% "
            "das tarefas atrasadas do mês.")
        resumo.append(f"{pct_top}% dos atrasos vêm de {len(top)} tipos de processo; começar por eles dá o maior retorno.")
        for tipo, l in top.iterrows():
            total_tipo = int((c["tipo"] == tipo).sum())
            acoes.append({
                "titulo": f"Atacar os atrasos de “{tipo}”",
                "porque": f"{int(l.qtd)} de {total_tipo} tarefas atrasaram ({_pct(int(l.qtd), total_tipo)}%), "
                          f"em média {_fmt_dias(l.media)} dias após o prazo. Verificar se o prazo do modelo é realista "
                          "e se há etapa travada (ex.: documento do cliente).",
                "responsavel": gestor_de(l.depto),
                "acompanhar": f"% no prazo de “{tipo}”",
                "peso": int(l.qtd),
            })

    disp = s[s["valor_novo"] == "dismissed"]
    if k["dispensadas"] >= max(20, 0.25 * k["concluidas"]):
        por_quem = disp["perfil_nome"].value_counts()
        por_tipo = disp["processo_titulo"].value_counts()
        quem, n_quem = por_quem.index[0], int(por_quem.iloc[0])
        porque["pessoas"].append(
            f"{k['dispensadas']} tarefas foram dispensadas no mês ({_pct(k['dispensadas'], k['concluidas'])}% do volume concluído); "
            f"{quem} fez {_pct(n_quem, k['dispensadas'])}% delas. Muitas dispensas no mesmo modelo indicam processo "
            "aplicado a clientes que não precisam dele.")
        tipo_disp = por_tipo.index[0]
        depto_disp = _moda(c.loc[c["tipo"] == tipo_disp, "departamento"])
        acoes.append({
            "titulo": f"Revisar para quais clientes “{tipo_disp}” é gerado",
            "porque": f"{int(por_tipo.iloc[0])} tarefas desse processo foram dispensadas no mês; "
                      "provavelmente o modelo é aplicado a clientes onde não se aplica.",
            "responsavel": gestor_de(depto_disp) if depto_disp else "Gestão do TaskHub (modelos)",
            "acompanhar": "Tarefas dispensadas por mês",
            "peso": int(por_tipo.iloc[0]),
        })

    de_outro = c[c["concluida_por_id"] != c["responsavel_id"]]
    pct_outro = _pct(len(de_outro), len(c))
    if pct_outro >= 30:
        maiores = de_outro["concluida_por"].value_counts().head(2)
        porque["pessoas"].append(
            f"{pct_outro}% das tarefas foram concluídas por alguém que não é o responsável cadastrado "
            f"(principalmente {_lista_texto(list(maiores.index))}). Ou o responsável nos modelos/carteira está "
            "desatualizado, ou essas pessoas estão absorvendo trabalho de outras; nos dois casos, as métricas "
            "por responsável ficam distorcidas.")
        acoes.append({
            "titulo": "Atualizar os responsáveis nos modelos e na carteira de clientes",
            "porque": f"{pct_outro}% das tarefas do mês foram fechadas por outra pessoa que não a responsável.",
            "responsavel": "Gestão do TaskHub (carteira de clientes)",
            "acompanhar": "% de tarefas concluídas pelo próprio responsável",
            "peso": len(de_outro) // 2,
        })

    equipe = k["pct_no_prazo"]
    pessoas = resumo_grupo(c, "concluida_por")
    volume = pessoas[pessoas["concluidas"] >= MIN_VOLUME]
    abaixo = volume[volume["pct"] <= equipe - 15]
    for nome, l in abaixo.head(3).iterrows():
        dela = atrasadas[atrasadas["concluida_por"] == nome]
        tipo = _moda(dela["tipo"])
        depto = _moda(c.loc[c["concluida_por"] == nome, "departamento"])
        porque["pessoas"].append(
            f"{nome}: {l.pct}% no prazo em {l.concluidas} tarefas (equipe: {equipe}%); os atrasos se concentram em “{tipo}”.")
        responsavel = gestor_de(depto)
        acoes.append({
            "titulo": f"Acompanhar a fila de {nome}",
            "porque": f"{l.pct}% no prazo contra {equipe}% da equipe, com {l.atrasadas} tarefas atrasadas "
                      f"(principalmente “{tipo}”). Verificar sobrecarga e redistribuir se for o caso.",
            "responsavel": responsavel if responsavel != nome else "Gestão",
            "acompanhar": f"% no prazo de {nome}",
            "peso": int(l.atrasadas),
        })
    if len(volume):
        melhor = volume.sort_values(["pct", "concluidas"], ascending=False).iloc[0]
        if melhor.pct >= equipe + 10:
            porque["pessoas"].append(
                f"Destaque positivo: {melhor.name} concluiu {melhor.concluidas} tarefas com {melhor.pct}% no prazo.")

    deptos = resumo_grupo(c, "departamento")
    deptos = deptos[deptos["concluidas"] >= MIN_VOLUME]
    if len(deptos) >= 2:
        ordenado = deptos.sort_values("pct")
        pior, melhor = ordenado.iloc[0], ordenado.iloc[-1]
        porque["departamentos"].append(
            f"{pior.name} tem a menor pontualidade ({pior.pct}% em {pior.concluidas} tarefas, atraso médio de "
            f"{_fmt_dias(pior.atraso_medio)} dias); {melhor.name}, a maior ({melhor.pct}%).")
    p = proc[proc["mes"] == mes]
    if len(p):
        por_dep = p.groupby("departamento")["no_prazo"].agg(["mean", "size"])
        por_dep = por_dep[por_dep["size"] >= 3].sort_values("mean")
        if len(por_dep) and por_dep["mean"].iloc[0] < 0.5:
            nome = por_dep.index[0]
            pct = round(por_dep["mean"].iloc[0] * 100)
            porque["departamentos"].append(
                f"Nos processos, {nome} entregou só {pct}% no prazo legal ({int(por_dep['size'].iloc[0])} processos concluídos). "
                "Vale conferir se os prazos desses processos estão configurados corretamente.")
            acoes.append({
                "titulo": f"Conferir prazos e fila dos processos de {nome}",
                "porque": f"Só {pct}% dos processos concluídos no mês ficaram dentro do prazo legal.",
                "responsavel": gestor_de(nome),
                "acompanhar": f"% de processos de {nome} no prazo legal",
                "peso": int(por_dep["size"].iloc[0]),
            })
    if k["reabertas"]:
        porque["evolucao"].append(f"{k['reabertas']} tarefas foram reabertas depois de concluídas (retrabalho).")

    acoes.sort(key=lambda a: -a["peso"])
    acoes = acoes[:6]
    if acoes:
        resumo.append(f"{len(acoes)} ações sugeridas no Plano de ação, ordenadas pelo número de tarefas envolvidas.")
    return porque, acoes, resumo



CSS_EXTRA = """
.kpi .valor { font-variant-numeric: normal; }
.kpi .delta.melhor { color: #0a7d0a; } .kpi .delta.pior { color: #b23131; }
.resumo { background: #f1f6fd; border-radius: 8px; padding: 10px 14px; margin: 12px 0 4px; }
.resumo h3 { margin: 0 0 4px; font-size: 10pt; }
.resumo ul { margin: 0; padding-left: 18px; } .resumo li { margin: 2px 0; }
.dupla { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.figura { break-inside: avoid; }
.figura h4 { margin: 4px 0 2px; font-size: 9pt; font-weight: 600; }
.figura .sub { color: var(--text-secondary); font-size: 8pt; margin-bottom: 4px; }
.legenda { display: flex; gap: 14px; font-size: 8pt; color: var(--text-secondary); margin: 2px 0 4px; }
.legenda i { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 5px; vertical-align: -1px; }
.porque { border-left: 3px solid #2a78d6; background: #fafaf8; padding: 7px 12px; margin: 8px 0 4px; break-inside: avoid; }
.porque b { display: block; font-size: 9pt; margin-bottom: 2px; }
.porque ul { margin: 0; padding-left: 16px; } .porque li { margin: 2px 0; }
.acoes { counter-reset: acao; }
.acao { position: relative; border: 1px solid var(--border); border-radius: 8px; padding: 9px 12px 9px 44px;
  margin-bottom: 7px; break-inside: avoid; }
.acao::before { counter-increment: acao; content: counter(acao); position: absolute; left: 12px; top: 10px;
  width: 22px; height: 22px; line-height: 22px; text-align: center; border-radius: 50%; background: #2a78d6;
  color: #fff; font-weight: 600; font-size: 9pt; }
.acao h3 { margin: 0 0 2px; font-size: 10pt; }
.acao .ev { color: var(--text-secondary); }
.acao .meta { display: flex; gap: 18px; font-size: 8.5pt; margin-top: 4px; color: var(--text-secondary); }
.acao .meta b { color: var(--text-primary); font-weight: 600; }
.grade-tipos { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.tipo { border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px; break-inside: avoid; }
.tipo h3 { margin: 0 0 4px; font-size: 9pt; }
.tipo table td, .tipo table th { padding: 2px 4px; }
.mini { display: inline-block; width: 46px; height: 6px; background: #f0efec; border-radius: 3px; vertical-align: middle; margin-right: 5px; }
.mini span { display: block; height: 6px; background: #0ca30c; border-radius: 3px; }
.pct-baixo { color: #b23131; font-weight: 600; }
.anexo { break-before: page; }
"""


def _classe_pct(valor):
    return " pct-baixo" if valor is not None and valor < 70 else ""


def _bloco_porque(frases):
    if not frases:
        return ""
    return '<div class="porque"><b>Por quê?</b><ul>' + "".join(f"<li>{escape(f)}</li>" for f in frases) + "</ul></div>"


def _figura(titulo, sub, conteudo, legenda=""):
    return f'<div class="figura"><h4>{escape(titulo)}</h4><div class="sub">{escape(sub)}</div>{legenda}{conteudo}</div>'


def _secao(titulo, corpo, sub=""):
    return f'<h2>{escape(titulo)}{f" <span class=qtd>· {escape(sub)}</span>" if sub else ""}</h2>{corpo}'


def gerar_html(conta, extracao, mes, conc, status, proc, cobertura, gestores, hoje):
    k = indicadores(conc, status, proc, mes)
    mes_ant = _mes_anterior(mes)
    ka = indicadores(conc, status, proc, mes_ant)
    porque, acoes, resumo = diagnosticar(conc, status, proc, mes, gestores)
    c = conc[conc["mes"] == mes]
    s = status[status["mes"] == mes]
    p = proc[proc["mes"] == mes]

    def delta(chave, maior_melhor=True, pontos=False):
        atual, antes = k[chave], ka[chave]
        if atual is None or antes is None or (isinstance(antes, int) and antes == 0 and not pontos):
            return ""
        dif = atual - antes
        if abs(dif) < 0.05:
            return f'<div class="delta">= {_mes_rotulo(mes_ant)}</div>'
        classe = "melhor" if (dif > 0) == maior_melhor else "pior"
        if pontos or isinstance(dif, int):
            texto = f"{'+' if dif > 0 else ''}{dif:.0f}{' p.p.' if pontos else ''}"
        else:
            texto = f"{'+' if dif > 0 else ''}{dif:.1f}".replace(".", ",")
        return f'<div class="delta {classe}">{texto} vs. {_mes_rotulo(mes_ant)}</div>'

    cards = [
        ("Tarefas concluídas", k["concluidas"], delta("concluidas")),
        ("Concluídas no prazo", _fmt_pct(k["pct_no_prazo"]), delta("pct_no_prazo", pontos=True)),
        ("Atraso médio das atrasadas", f"{_fmt_dias(k['atraso_medio'])} d" if k["atraso_medio"] else "—", delta("atraso_medio", maior_melhor=False)),
        ("Tarefas dispensadas", k["dispensadas"], delta("dispensadas", maior_melhor=False)),
        ("Tarefas reabertas", k["reabertas"], delta("reabertas", maior_melhor=False)),
        ("Processos concluídos", k["processos"], delta("processos")),
        ("Processos no prazo legal", _fmt_pct(k["pct_proc_prazo"]), delta("pct_proc_prazo", pontos=True)),
        ("Processos no prazo meta", _fmt_pct(k["pct_proc_meta"]), delta("pct_proc_meta", pontos=True)),
    ]
    kpis = "".join(f'<div class="kpi"><div class="rotulo">{r}</div><div class="valor">{v}</div>{d}</div>' for r, v, d in cards)
    bloco_resumo = ('<div class="resumo"><h3>Resumo do mês</h3><ul>' + "".join(f"<li>{escape(r)}</li>" for r in resumo)
                    + "</ul></div>") if resumo else ""

    meses = [_mes_anterior(mes, n) for n in range(MESES_TENDENCIA - 1, -1, -1)]
    serie = [(m, indicadores(conc, status, proc, m)) for m in meses]
    while len(serie) > 1 and serie[0][1]["concluidas"] == 0:
        serie.pop(0)
    rotulos_meses = [_mes_rotulo(m)[:3] for m, _ in serie]
    g_volume = gf.colunas([(r, i["concluidas"]) for r, (_, i) in zip(rotulos_meses, serie)], "Tarefas concluídas por mês")
    series_pontual = [("Tarefas no prazo", gf.AZUL, [i["pct_no_prazo"] for _, i in serie]),
                      ("Processos no prazo legal", gf.LARANJA, [i["pct_proc_prazo"] for _, i in serie])]
    g_pontual = gf.linhas_percentuais(rotulos_meses, series_pontual, "Pontualidade por mês")
    sec_evolucao = (
        f'<div class="dupla">{_figura("Volume", "tarefas concluídas por mês", g_volume)}'
        f'{_figura("Pontualidade", "% concluído dentro do prazo", g_pontual, gf.legenda([(n, cor) for n, cor, _ in series_pontual]))}</div>'
        + _bloco_porque(porque["evolucao"])
    )

    faixas = []
    for rotulo, minimo, maximo in FAIXAS_ATRASO:
        if minimo is None:
            n = int((c["dif"] <= 0).sum())
        elif maximo is None:
            n = int((c["dif"] >= minimo).sum())
        else:
            n = int(c["dif"].between(minimo, maximo).sum())
        faixas.append((rotulo, n))
    g_faixas = gf.colunas(faixas, "Conclusão em relação ao prazo", cores=[gf.BOM] + [gf.SERIO] * (len(faixas) - 1))
    atrasadas = c[~c["no_prazo"]]
    por_tipo = atrasadas.groupby("tipo").agg(qtd=("id", "size"), media=("dif", "mean")).sort_values("qtd", ascending=False).head(7)
    g_tipos = gf.barras_horizontais(
        [(t, int(l.qtd)) for t, l in por_tipo.iterrows()], "Tipos de processo com mais tarefas atrasadas",
        largura=380, rotulo_largura=160, cor=gf.SERIO,
        extra=[f"· {_pct(int(l.qtd), len(atrasadas))}% · {_fmt_dias(l.media)} d" for _, l in por_tipo.iterrows()])
    sec_atrasos = (
        f'<div class="dupla">{_figura("Quando as tarefas foram concluídas", "dias após o prazo da tarefa", g_faixas, gf.legenda([("No prazo", gf.BOM), ("Atrasada", gf.SERIO)]))}'
        f'{_figura("Onde se concentram os atrasos", "tarefas atrasadas · % do total · atraso médio", g_tipos)}</div>'
        + _bloco_porque(porque["atrasos"])
    )

    pessoas = resumo_grupo(c, "concluida_por")
    g_pessoas = gf.barras_empilhadas(
        list(pessoas.index),
        [("No prazo", gf.BOM, [int(v) for v in pessoas["no_prazo"]]),
         ("Atrasada", gf.SERIO, [int(v) for v in pessoas["atrasadas"]])],
        "Tarefas concluídas por pessoa", total_extra=[f"{v}% no prazo" for v in pessoas["pct"]])
    sec_pessoas = (
        _figura("Tarefas concluídas por pessoa", "quem concluiu a tarefa no Hubstrom · total e % no prazo", g_pessoas,
                gf.legenda([("No prazo", gf.BOM), ("Atrasada", gf.SERIO)]))
        + _bloco_porque(porque["pessoas"])
    )

    deptos = resumo_grupo(c, "departamento")
    g_deptos = gf.barras_horizontais(
        [(n, int(l.pct)) for n, l in deptos.iterrows()], "Pontualidade das tarefas por departamento",
        largura=380, rotulo_largura=110, maximo=100, formato=lambda v: f"{v}%",
        extra=[f"· {int(l.concluidas)} tarefas" for _, l in deptos.iterrows()])
    proc_dep = sorted(((nome, _pct(int(g["no_prazo"].sum()), len(g)), len(g)) for nome, g in p.groupby("departamento")),
                      key=lambda x: -x[2])
    g_proc = gf.barras_horizontais(
        [(n, v or 0) for n, v, _ in proc_dep], "Processos no prazo legal por departamento",
        largura=380, rotulo_largura=110, maximo=100, formato=lambda v: f"{v}%", cor=gf.LARANJA,
        extra=[f"· {q} processos" for _, _, q in proc_dep])
    sec_deptos = (
        f'<div class="dupla">{_figura("Tarefas", "% concluídas no prazo · volume", g_deptos)}'
        f'{_figura("Processos", "% concluídos no prazo legal · volume", g_proc) if proc_dep else ""}</div>'
        + _bloco_porque(porque["departamentos"])
    )

    tipos = resumo_grupo(c, "tipo").head(10)
    blocos = []
    for tipo, lt in tipos.iterrows():
        por_pessoa = resumo_grupo(c[c["tipo"] == tipo], "concluida_por")
        por_pessoa = por_pessoa[por_pessoa["concluidas"] >= MIN_TAREFAS_COMPARACAO]
        corpo = "".join(
            f"<tr><td>{escape(n)}</td><td class='n'>{l.concluidas}</td>"
            f"<td class='n{_classe_pct(l.pct)}'><span class='mini'><span style='width:{l.pct}%'></span></span>{l.pct}%</td>"
            f"<td class='n'>{_fmt_dias(l.atraso_medio)}</td></tr>"
            for n, l in por_pessoa.iterrows()
        ) or f"<tr><td colspan='4' class='vazio'>Ninguém com {MIN_TAREFAS_COMPARACAO}+ tarefas deste tipo.</td></tr>"
        blocos.append(
            f"<div class='tipo'><h3>{escape(tipo)} · {lt.concluidas} tarefas · {lt.pct}% no prazo</h3>"
            "<table><thead><tr><th>Quem concluiu</th><th class='n'>Qtd.</th><th class='n'>No prazo</th>"
            f"<th class='n'>Atraso méd.</th></tr></thead><tbody>{corpo}</tbody></table></div>"
        )
    sec_tipos = f"<div class='grade-tipos'>{''.join(blocos)}</div>" if blocos else '<p class="vazio">Sem dados no período.</p>'

    cards_acao = "".join(
        f'<div class="acao"><h3>{escape(a["titulo"])}</h3><div class="ev"><b>Por quê:</b> {escape(a["porque"])}</div>'
        f'<div class="meta"><span>Responsável sugerido: <b>{escape(a["responsavel"])}</b></span>'
        f'<span>Como acompanhar: <b>{escape(a["acompanhar"])}</b></span></div></div>'
        for a in acoes
    ) or '<p class="vazio">Nenhuma ação sugerida: os indicadores do mês não acenderam nenhum alerta.</p>'
    sec_acoes = f'<div class="acoes">{cards_acao}</div>'

    dispensas = s[s["valor_novo"] == "dismissed"].groupby("perfil_nome").size()
    de_outro = c[c["concluida_por_id"] != c["responsavel_id"]].groupby("concluida_por").size()
    linhas = "".join(
        f"<tr><td>{escape(nome)}</td><td class='n'>{l.concluidas}</td>"
        f"<td class='n{_classe_pct(l.pct)}'>{l.pct}%</td>{_celula_n(int(l.atrasadas))}"
        f"<td class='n'>{_fmt_dias(l.atraso_medio)}</td>{_celula_n(int(dispensas.get(nome, 0)))}"
        f"{_celula_n(int(de_outro.get(nome, 0)))}</tr>"
        for nome, l in pessoas.iterrows()
    )
    tab_pessoas = (
        "<table><thead><tr><th>Quem concluiu</th><th class='n'>Concluídas</th><th class='n'>No prazo</th>"
        "<th class='n'>Atrasadas</th><th class='n'>Atraso médio (dias)</th><th class='n'>Dispensadas</th>"
        f"<th class='n'>De outro responsável</th></tr></thead><tbody>{linhas}</tbody></table>"
    )
    linhas = "".join(
        f"<tr><td>{escape(nome)}</td><td class='n'>{l.concluidas}</td><td class='n{_classe_pct(l.pct)}'>{l.pct}%</td>"
        f"{_celula_n(int(l.atrasadas))}<td class='n'>{_fmt_dias(l.atraso_medio)}</td><td>{escape(gestores.get(nome) or '—')}</td></tr>"
        for nome, l in deptos.iterrows()
    )
    tab_deptos = (
        "<table><thead><tr><th>Departamento</th><th class='n'>Concluídas</th><th class='n'>No prazo</th>"
        f"<th class='n'>Atrasadas</th><th class='n'>Atraso médio (dias)</th><th>Gestor(a)</th></tr></thead><tbody>{linhas}</tbody></table>"
    )
    linhas = []
    for nome, g in p.groupby("departamento"):
        metas = g["na_meta"].dropna()
        pct_p = _pct(int(g["no_prazo"].sum()), len(g))
        linhas.append((len(g), f"<tr><td>{escape(nome)}</td><td class='n'>{len(g)}</td>"
                       f"<td class='n{_classe_pct(pct_p)}'>{_fmt_pct(pct_p)}</td>"
                       f"<td class='n'>{_fmt_pct(_pct(int(metas.astype(bool).sum()), len(metas)))}</td></tr>"))
    tab_proc = (
        "<table><thead><tr><th>Departamento</th><th class='n'>Processos concluídos</th><th class='n'>No prazo legal</th>"
        f"<th class='n'>No prazo meta</th></tr></thead><tbody>{''.join(l for _, l in sorted(linhas, reverse=True))}</tbody></table>"
    ) if linhas else '<p class="vazio">Nenhum processo concluído no período.</p>'
    linhas = "".join(
        f"<tr><td>{_mes_rotulo(m)}</td>{_celula_n(i['concluidas'])}<td class='n'>{_fmt_pct(i['pct_no_prazo'])}</td>"
        f"<td class='n'>{_fmt_dias(i['atraso_medio'])}</td>{_celula_n(i['dispensadas'])}{_celula_n(i['reabertas'])}"
        f"{_celula_n(i['processos'])}<td class='n'>{_fmt_pct(i['pct_proc_prazo'])}</td></tr>"
        for m, i in serie
    )
    tab_tendencia = (
        "<table><thead><tr><th>Mês</th><th class='n'>Concluídas</th><th class='n'>No prazo</th><th class='n'>Atraso médio</th>"
        "<th class='n'>Dispensadas</th><th class='n'>Reabertas</th><th class='n'>Processos</th><th class='n'>Proc. no prazo legal</th>"
        f"</tr></thead><tbody>{linhas}</tbody></table>"
    )

    extraido = datetime.fromisoformat(extracao["extraido_em"]).astimezone(FUSO)
    periodo = _mes_rotulo(mes) + (f" (até {hoje:%d/%m})" if mes == f"{hoje.year}{hoje.month:02d}" else "")
    inicio = cobertura["inicio_historico"]
    cobertura_mes = cobertura["por_mes_prazo"].get(mes, (None, 0))
    return f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<title>Desempenho da equipe — {escape(conta['nome_conta'])}</title><style>{CSS}{CSS_EXTRA}</style></head><body>
<div class="cabecalho"><div><h1>Desempenho da equipe</h1>
<div class="sub">{escape(conta['nome_conta'])} · {periodo}</div></div>
<div class="extraido">Dados extraídos do Hubstrom em {extraido:%d/%m/%Y às %H:%M}</div></div>
<div class="kpis">{kpis}</div>
{bloco_resumo}
{_secao("1. Como o mês evoluiu", sec_evolucao)}
{_secao("2. Onde estão os atrasos", sec_atrasos)}
{_secao("3. Pessoas", sec_pessoas)}
{_secao("4. Departamentos", sec_deptos)}
{_secao("5. Comparação dentro do mesmo tipo de processo", sec_tipos, f"os 10 tipos com mais tarefas; pessoas com {MIN_TAREFAS_COMPARACAO}+ tarefas")}
{_secao("6. Plano de ação", sec_acoes, "sugestões geradas a partir dos dados, por ordem de impacto")}
<div class="anexo">
{_secao("Anexo · Por pessoa", tab_pessoas)}
{_secao("Anexo · Por departamento", tab_deptos)}
{_secao("Anexo · Processos concluídos por departamento", tab_proc, "conclusão = data da última tarefa")}
{_secao("Anexo · Evolução mensal", tab_tendencia)}
<dl class="nota">
<dt>Fonte:</dt><dd>Histórico de ações do Hubstrom (desde {inicio:%d/%m/%Y}), ligado às tarefas por cliente e títulos.
{cobertura['logs_ligados']}% dos registros de status foram ligados com segurança. Das {cobertura_mes[1]} tarefas concluídas
com prazo em {_mes_rotulo(mes)}, {_fmt_pct(cobertura_mes[0])} têm data de conclusão identificada; as demais (principalmente
tarefas automáticas, concluídas pelo próprio sistema, e registros ambíguos) ficam fora das métricas de pontualidade.</dd>
<dt>Por quê? e Plano de ação:</dt><dd>gerados automaticamente por regras sobre os dados; são indícios para orientar a conversa, não conclusões.</dd>
<dt>No prazo:</dt><dd>tarefa concluída até o dia do prazo da tarefa (dia útil sem considerar feriados).</dd>
<dt>Atraso médio:</dt><dd>média de dias após o prazo, só entre as concluídas com atraso.</dd>
<dt>Dispensadas e reabertas:</dt><dd>contadas em todos os registros do histórico do mês; reaberta = voltou de Concluído para A fazer/Em andamento.</dd>
<dt>De outro responsável:</dt><dd>tarefa concluída por alguém que não é o responsável cadastrado.</dd>
<dt>Processos:</dt><dd>só entram quando todas as suas tarefas concluídas têm data identificada.</dd>
Quantidade de tarefas não mede esforço: tarefas variam muito de tamanho, por isso compare pessoas dentro do mesmo tipo de processo.
</dl></div></body></html>"""


def gerar(indices, mes=None):
    hoje = datetime.now(FUSO).date()
    mes = mes or f"{hoje.year}{hoje.month:02d}"
    pasta = os.path.join(PASTA_RELATORIOS, hoje.isoformat())
    os.makedirs(pasta, exist_ok=True)
    saidas = []
    for indice in indices:
        conta = CONTAS[indice]
        extracao = banco.ultima_extracao(conta["subpasta"])
        if not extracao:
            log.error("[%s] Nenhuma extração no banco; rode o extrator primeiro.", conta["nome_conta"])
            continue
        conc, status, proc, cobertura, gestores = preparar(carregar(extracao["id"]))
        html = gerar_html(conta, extracao, mes, conc, status, proc, cobertura, gestores, hoje)
        caminho = os.path.join(pasta, f"Desempenho_{conta['subpasta']}_{mes}.pdf")
        with open(caminho[:-4] + ".html", "w", encoding="utf-8") as f:
            f.write(html)
        saidas.append((caminho, html, f"Desempenho da equipe · {conta['nome_conta']} · {_mes_rotulo(mes)}"))
    gerar_pdf(saidas)
    return [s[0] for s in saidas]


if __name__ == "__main__":
    argumentos = sys.argv[1:]
    mes = argumentos[argumentos.index("--mes") + 1] if "--mes" in argumentos else None
    indices = [int(a) for a in argumentos if a.isdigit() and a != mes] or list(range(len(CONTAS)))
    if "--extrair" in argumentos:
        import extrator
        extrator.main(indices)
    gerar(indices, mes)
