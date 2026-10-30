import collections
import re
import unicodedata

import pandas as pd

MARGEM_DESEMPATE_DIAS = 10


def normalizar(texto):
    texto = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode().upper()
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9]+", " ", texto)).strip()


def so_digitos(texto):
    return re.sub(r"\D", "", str(texto or ""))


def ligar_logs_de_tarefas(historico, tarefas, processos, clientes):
    """Devolve os logs de status de tarefas com as colunas `tarefa_id` e `ligacao`
    (unico / data / prazo / ambiguo / sem_par / sem_cliente)."""
    logs = historico[(historico["tipo"] == "tarefa") & (historico["tipo_log"] == "status")].copy()
    logs["quando_dt"] = pd.to_datetime(logs["quando"], utc=True, errors="coerce")

    clientes_por_doc = collections.defaultdict(set)
    for cid, documento in zip(clientes["id"], clientes["documento"]):
        if so_digitos(documento):
            clientes_por_doc[so_digitos(documento)].add(cid)

    proc = processos[["id", "titulo", "criado_em", "prazo"]].rename(
        columns={"id": "processo_id", "titulo": "titulo_processo", "prazo": "prazo_processo"})
    t = tarefas.merge(proc, on="processo_id", how="left")
    t["criado_dt"] = pd.to_datetime(t["criado_em"], utc=True, errors="coerce")
    t["prazo_proc_dt"] = pd.to_datetime(t["prazo_processo"], utc=True, errors="coerce")
    indice = collections.defaultdict(list)
    for linha in t.itertuples():
        indice[(linha.cliente_id, normalizar(linha.titulo_processo), normalizar(linha.titulo))].append(linha)

    ids, ligacoes = [], []
    for log in logs.itertuples():
        clientes_log = clientes_por_doc.get(so_digitos(log.cliente_doc), set())
        if not clientes_log:
            ids.append(None), ligacoes.append("sem_cliente")
            continue
        candidatos = [c for cid in clientes_log
                      for c in indice.get((cid, normalizar(log.processo_titulo), normalizar(log.tarefa_titulo)), [])]
        if not candidatos:
            ids.append(None), ligacoes.append("sem_par")
            continue
        if len(candidatos) == 1:
            ids.append(candidatos[0].id), ligacoes.append("unico")
            continue
        vivos = [c for c in candidatos if pd.notna(c.criado_dt) and c.criado_dt <= log.quando_dt]
        if len(vivos) == 1:
            ids.append(vivos[0].id), ligacoes.append("data")
            continue
        com_prazo = sorted((c for c in vivos if pd.notna(c.prazo_proc_dt)),
                           key=lambda c: abs((c.prazo_proc_dt - log.quando_dt).total_seconds()))
        if len(com_prazo) >= 2:
            d0 = abs((com_prazo[0].prazo_proc_dt - log.quando_dt).days)
            d1 = abs((com_prazo[1].prazo_proc_dt - log.quando_dt).days)
            if d1 - d0 >= MARGEM_DESEMPATE_DIAS:
                ids.append(com_prazo[0].id), ligacoes.append("prazo")
                continue
        ids.append(None), ligacoes.append("ambiguo")

    logs["tarefa_id"] = ids
    logs["ligacao"] = ligacoes
    return logs


def conclusoes_de_tarefas(logs_ligados, tarefas):
    """Para cada tarefa hoje concluída, a data e o autor da conclusão (último log →completed).
    Tarefas cujo último log não é de conclusão ficam de fora (o histórico não bate)."""
    ligados = logs_ligados[logs_ligados["tarefa_id"].notna()].sort_values("quando_dt")
    ultimo = ligados.groupby("tarefa_id").tail(1).set_index("tarefa_id")
    concluidas = tarefas[tarefas["status"] == "completed"].set_index("id")
    comum = concluidas.index.intersection(ultimo.index)
    ult = ultimo.loc[comum]
    validos = ult[ult["valor_novo"] == "completed"]
    resultado = concluidas.loc[validos.index].copy()
    resultado["concluida_em"] = validos["quando_dt"]
    resultado["concluida_por_id"] = validos["perfil_id"]
    resultado["concluida_por"] = validos["perfil_nome"]
    resultado.index.name = "id"
    return resultado.reset_index()
