import os
import sqlite3
from datetime import datetime

from config import PASTA_PROJETO

CAMINHO_BANCO = os.path.join(PASTA_PROJETO, "dados", "hubstrom.db")

TABELAS = {
    "departamentos": ["id", "nome", "gestor_id"],
    "colaboradores": ["id", "nome", "email", "cargo"],
    "clientes": ["id", "nome", "documento", "regime", "cidade", "uf", "honorario", "status",
                 "ultimo_acesso_app", "responsaveis"],
    "processos": ["id", "mes_prazo", "cliente_id", "titulo", "status", "prazo", "prazo_meta", "criado_em",
                  "competencia", "tem_multa", "responsavel_id", "departamento_id", "departamentos",
                  "responsaveis_tarefas", "modelo_id"],
    "tarefas": ["id", "mes_prazo", "processo_id", "cliente_id", "titulo", "status", "prazo", "prioridade",
                "responsavel_id", "departamento_id", "qtd_comentarios", "qtd_documentos"],
    "ordens_servico": ["id", "codigo", "cliente_id", "processo_id", "tipo", "status", "criado_em",
                       "responsaveis", "departamentos"],
    "historico": ["id", "tipo", "quando", "acao", "tipo_log", "perfil_id", "perfil_nome", "perfil_cargo",
                  "cliente_doc", "processo_titulo", "tarefa_titulo", "valor_anterior", "valor_novo"],
}


def conectar():
    os.makedirs(os.path.dirname(CAMINHO_BANCO), exist_ok=True)
    conexao = sqlite3.connect(CAMINHO_BANCO)
    conexao.row_factory = sqlite3.Row
    conexao.execute(
        "CREATE TABLE IF NOT EXISTS extracoes ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, conta TEXT NOT NULL, nome_conta TEXT, extraido_em TEXT NOT NULL)"
    )
    for tabela, colunas in TABELAS.items():
        conexao.execute(
            f"CREATE TABLE IF NOT EXISTS {tabela} (extracao_id INTEGER NOT NULL REFERENCES extracoes(id), "
            + ", ".join(colunas) + ", PRIMARY KEY (extracao_id, id))"
        )
        conexao.execute(f"CREATE INDEX IF NOT EXISTS ix_{tabela}_extracao ON {tabela}(extracao_id)")
    return conexao


def gravar_extracao(conta, nome_conta, dados):
    with conectar() as conexao:
        cursor = conexao.execute(
            "INSERT INTO extracoes (conta, nome_conta, extraido_em) VALUES (?, ?, ?)",
            (conta, nome_conta, datetime.now().astimezone().isoformat(timespec="seconds")),
        )
        extracao_id = cursor.lastrowid
        for tabela, colunas in TABELAS.items():
            conexao.executemany(
                f"INSERT OR REPLACE INTO {tabela} (extracao_id, {', '.join(colunas)}) "
                f"VALUES (?, {', '.join('?' * len(colunas))})",
                [(extracao_id, *(linha.get(c) for c in colunas)) for linha in dados.get(tabela, [])],
            )
    return extracao_id


def ultima_extracao(conta):
    with conectar() as conexao:
        linha = conexao.execute(
            "SELECT * FROM extracoes WHERE conta = ? ORDER BY id DESC LIMIT 1", (conta,)
        ).fetchone()
    return dict(linha) if linha else None
