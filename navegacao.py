import os
from datetime import datetime

from config import PASTA_DIAGNOSTICO, log

OPCOES_FILTRO_PRAZO = {"Todos", "Hoje", "Esta semana", "A atrasar", "Atrasados", "Em multa"}


def texto_seguro(localizador, timeout=None):
    try:
        return localizador.inner_text(timeout=timeout).strip()
    except Exception:
        return ""


def fechar_popups(page):
    try:
        fechar = page.locator('button.close-button')
        total = fechar.count()
        for i in range(total - 1, -1, -1):
            try:
                fechar.nth(i).click(timeout=1500)
                log.info("Fechei um pop-up sobre a tela.")
                page.wait_for_timeout(500)
                return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def clicar_no_menu(page, seletor, tentativas=3, nome="item do menu"):
    fechar_popups(page)
    for tentativa in range(1, tentativas + 1):
        try:
            page.locator(seletor).first.click(timeout=10000)
            return
        except Exception:
            log.info("Clique em '%s' foi bloqueado, tentando de novo (%d/%d).", nome, tentativa, tentativas)
            fechar_popups(page)
    page.locator(seletor).first.click(timeout=10000)


def aguardar_rede(page, timeout=15000):
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        pass


def clicar_ate_confirmar(page, localizador_botao, verificar, tentativas=3, espera_segundos=6, nome="botão"):
    for tentativa in range(1, tentativas + 1):
        try:
            localizador_botao.click()
        except Exception:
            if verificar():
                return
        for _ in range(int(espera_segundos * 2)):
            page.wait_for_timeout(500)
            if verificar():
                return
        log.info("%s não avançou a tela na tentativa %d/%d, clicando de novo...", nome, tentativa, tentativas)
    if verificar():
        return
    raise TimeoutError(f"'{nome}' não avançou a tela mesmo depois de {tentativas} cliques.")


def salvar_diagnostico(page, rotulo):
    try:
        carimbo = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = os.path.join(PASTA_DIAGNOSTICO, f"{rotulo}_{carimbo}")
        page.screenshot(path=base + ".png", full_page=True)
        with open(base + ".html", "w", encoding="utf-8") as f:
            f.write(page.content())
        log.warning("Diagnóstico salvo em: %s.png / .html", base)
        return base
    except Exception:
        log.warning("Não consegui salvar o diagnóstico (a página pode já ter fechado).")
        return None


def fazer_login(page, conta):
    log.info("[%s] Acessando página de login.", conta["nome_conta"])
    page.goto("https://app.hubstrom.com/login", wait_until="domcontentloaded")
    page.locator('div[type="email"] input').fill(conta["email"])
    page.locator('input[type="password"]').fill(conta["senha"])
    clicar_ate_confirmar(
        page,
        page.locator('button[type="submit"]'),
        verificar=lambda: page.locator('button.sp-card').count() > 0,
        espera_segundos=45,
        nome="botão de entrar (login)",
    )
    page.locator('button.sp-card', has_text=conta["perfil_botao"]).click()
    page.wait_for_selector('input[placeholder="Digite a senha"]', timeout=30000)
    page.locator('input[placeholder="Digite a senha"]').fill(conta["pin"])
    clicar_ate_confirmar(
        page,
        page.locator('button[type="button"]:has-text("Acessar conta")'),
        verificar=lambda: page.locator('.sp-password-view').count() == 0,
        espera_segundos=15,
        nome="botão 'Acessar conta'",
    )
    page.wait_for_timeout(3000)
    log.info("[%s] Login concluído.", conta["nome_conta"])


def fazer_logout(page, conta):
    try:
        clicar_no_menu(page, 'button.header-user-trigger', nome="menu do usuário")
        page.wait_for_selector('button.header-dropdown-item--danger', timeout=10000)
        page.locator('button.header-dropdown-item--danger').click()
        page.wait_for_url("**/login**", timeout=15000)
        log.info("[%s] Logout realizado com sucesso.", conta["nome_conta"])
    except Exception:
        log.warning("[%s] Não consegui confirmar o logout.", conta["nome_conta"])


def abrir_taskhub(page):
    fechar_popups(page)
    clicar_no_menu(page, 'a[href^="/taskhub"]', nome="ícone do TaskHub")
    aguardar_rede(page)
    for tentativa in range(3):
        if page.locator('text=Descubra o gestor de processos inteligente').count() == 0:
            break
        log.info("Tela de propaganda do TaskHub, tentando de novo (%d/3).", tentativa + 1)
        clicar_no_menu(page, 'a[href^="/taskhub"]', nome="ícone do TaskHub")
        page.wait_for_timeout(3000)
    page.wait_for_timeout(2000)


def selecionar_filtro_prazo(page, valor="Todos"):
    botoes = page.locator('button.task-dropdown-button')
    for i in range(botoes.count()):
        fechar_popups(page)
        botao = botoes.nth(i)
        try:
            if not botao.is_visible():
                continue
            botao.click(timeout=10000)
        except Exception:
            continue
        page.wait_for_timeout(300)
        container = botao.locator("xpath=..")
        itens = container.locator('.dropdown-menu .dropdown-item .menu-sm')
        textos = {texto_seguro(itens.nth(j)) for j in range(itens.count())}
        if not OPCOES_FILTRO_PRAZO.issubset(textos):
            try:
                botao.click(timeout=5000)
            except Exception:
                pass
            page.wait_for_timeout(200)
            continue
        container.locator('.dropdown-item', has_text=valor).first.click(timeout=5000)
        log.info("Filtro 'Prazo dos processos' em '%s'.", valor)
        page.wait_for_timeout(500)
        return True
    log.warning("Não encontrei o filtro 'Prazo dos processos' nesta tela.")
    return False


def clicar_aba_visao(page, nome_aba, timeout=15000):
    fechar_popups(page)
    grupos = page.locator('.tab-group-wrapper')
    for i in range(grupos.count()):
        botoes = grupos.nth(i).locator('.desktop-tabs button.small-title')
        textos = [texto_seguro(botoes.nth(j)) for j in range(botoes.count())]
        if not {"Cliente", "Tarefa"}.issubset(set(textos)):
            continue
        for j, texto in enumerate(textos):
            if texto.lower() != nome_aba.strip().lower():
                continue
            botao = botoes.nth(j)
            if "active" not in (botao.get_attribute("class") or ""):
                botao.click(timeout=timeout)
                page.wait_for_timeout(2000)
            log.info("Aba '%s' selecionada.", nome_aba)
            return
    raise RuntimeError(f"Não encontrei a aba '{nome_aba}' no grupo Cliente/Processo/Tarefa da tela de Processos.")


def abrir_processos_cliente(page):
    clicar_no_menu(page, 'button.small-title:has-text("Processos")', nome="botão Processos (menu superior)")
    aguardar_rede(page)
    fechar_popups(page)
    clicar_no_menu(page, 'button.tab-button:has-text("Tabela")', nome="aba Tabela")
    page.wait_for_selector('button.task-dropdown-button', timeout=30000)
    fechar_popups(page)
    clicar_aba_visao(page, "Cliente")
    aguardar_rede(page)
