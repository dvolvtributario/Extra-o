from html import escape

FONTE = "Segoe UI, system-ui, sans-serif"
TEXTO = "#0b0b0b"
TEXTO_2 = "#52514e"
TEXTO_3 = "#7a7974"
GRADE = "#e4e3df"
FUNDO = "#ffffff"

AZUL = "#2a78d6"      
LARANJA = "#eb6834"   
BOM = "#0ca30c"       
SERIO = "#ec835a"     
CRITICO = "#d03b3b"


def _cortar(texto, limite):
    texto = str(texto)
    return texto if len(texto) <= limite else texto[: limite - 1].rstrip() + "…"


def _barra_h(x, y, largura, altura, cor):
    """Barra horizontal: base reta à esquerda, ponta arredondada (4px) à direita."""
    if largura <= 0:
        return ""
    r = min(4, altura / 2, largura)
    return (f'<path d="M{x:.1f},{y:.1f} h{largura - r:.1f} a{r},{r} 0 0 1 {r},{r} v{altura - 2 * r:.1f} '
            f'a{r},{r} 0 0 1 -{r},{r} h-{largura - r:.1f} z" fill="{cor}"/>')


def _coluna(x, y_base, largura, altura, cor):
    """Coluna vertical: base reta embaixo, ponta arredondada (4px) em cima."""
    if altura <= 0:
        return ""
    r = min(4, largura / 2, altura)
    return (f'<path d="M{x:.1f},{y_base:.1f} v-{altura - r:.1f} a{r},{r} 0 0 1 {r},-{r} h{largura - 2 * r:.1f} '
            f'a{r},{r} 0 0 1 {r},{r} v{altura - r:.1f} z" fill="{cor}"/>')


def _svg(largura, altura, corpo, titulo):
    return (f'<svg viewBox="0 0 {largura} {altura}" width="100%" role="img" aria-label="{escape(titulo)}" '
            f'style="font-family:{FONTE}; display:block">{corpo}</svg>')


def legenda(itens):
    """Legenda em HTML: [(nome, cor), ...]."""
    return '<div class="legenda">' + "".join(
        f'<span><i style="background:{cor}"></i>{escape(nome)}</span>' for nome, cor in itens
    ) + "</div>"


def barras_horizontais(itens, titulo, largura=700, formato=str, cor=AZUL, rotulo_largura=190, maximo=None, extra=None):
    """itens: [(rótulo, valor)]. `extra`: texto opcional ao lado do valor, por item."""
    if not itens:
        return ""
    linha, barra = 22, 12
    altura = linha * len(itens) + 6
    area = largura - rotulo_largura - (120 if extra else 50)
    maximo = maximo or max(v for _, v in itens) or 1
    limite = max(10, rotulo_largura // 7)  # maiúsculas ocupam ~7px por letra
    partes = [f'<line x1="{rotulo_largura}" y1="0" x2="{rotulo_largura}" y2="{altura}" stroke="{GRADE}" stroke-width="1"/>']
    for i, (rotulo, valor) in enumerate(itens):
        y = i * linha + 4
        w = max(0, valor) / maximo * area
        partes.append(f'<text x="{rotulo_largura - 8}" y="{y + barra - 2}" text-anchor="end" font-size="10.5" fill="{TEXTO}">'
                      f'<title>{escape(rotulo)}</title>{escape(_cortar(rotulo, limite))}</text>')
        partes.append(_barra_h(rotulo_largura, y, w, barra, cor))
        texto = formato(valor) + (f' <tspan fill="{TEXTO_3}">{escape(extra[i])}</tspan>' if extra else "")
        partes.append(f'<text x="{rotulo_largura + w + 6}" y="{y + barra - 2}" font-size="10" fill="{TEXTO_2}">{texto}</text>')
    return _svg(largura, altura, "".join(partes), titulo)


def barras_empilhadas(rotulos, series, titulo, largura=700, rotulo_largura=190, total_extra=None):
    """series: [(nome, cor, [valores])]. Segmentos separados por 2px de fundo; total no fim da barra."""
    if not rotulos:
        return ""
    linha, barra, vao = 22, 12, 2
    altura = linha * len(rotulos) + 6
    area = largura - rotulo_largura - (130 if total_extra else 50)
    totais = [sum(s[2][i] for s in series) for i in range(len(rotulos))]
    maximo = max(totais) or 1
    partes = [f'<line x1="{rotulo_largura}" y1="0" x2="{rotulo_largura}" y2="{altura}" stroke="{GRADE}" stroke-width="1"/>']
    for i, rotulo in enumerate(rotulos):
        y = i * linha + 4
        partes.append(f'<text x="{rotulo_largura - 8}" y="{y + barra - 2}" text-anchor="end" font-size="10.5" fill="{TEXTO}">'
                      f'{escape(_cortar(rotulo, 32))}</text>')
        x = rotulo_largura
        visiveis = [(cor, valores[i]) for _, cor, valores in series if valores[i] > 0]
        for j, (cor, valor) in enumerate(visiveis):
            w = valor / maximo * area
            ultimo = j == len(visiveis) - 1
            if ultimo:
                partes.append(_barra_h(x, y, w, barra, cor))
            else:
                partes.append(f'<rect x="{x:.1f}" y="{y}" width="{max(0, w - vao):.1f}" height="{barra}" fill="{cor}"/>')
            x += w
        rotulo_total = str(totais[i]) + (f" · {total_extra[i]}" if total_extra else "")
        partes.append(f'<text x="{x + 6:.1f}" y="{y + barra - 2}" font-size="10" fill="{TEXTO_2}">{escape(rotulo_total)}</text>')
    return _svg(largura, altura, "".join(partes), titulo)


def colunas(itens, titulo, largura=340, altura=170, cores=None):
    """itens: [(rótulo, valor)]. Valor no topo de cada coluna."""
    if not itens:
        return ""
    topo, base_txt = 18, 30
    area_h = altura - topo - base_txt
    banda = (largura - 10) / len(itens)
    w = min(24, banda * 0.55)
    maximo = max(v for _, v in itens) or 1
    y_base = topo + area_h
    partes = [f'<line x1="0" y1="{y_base}" x2="{largura}" y2="{y_base}" stroke="{GRADE}" stroke-width="1"/>']
    for i, (rotulo, valor) in enumerate(itens):
        cx = 5 + banda * i + banda / 2
        h = valor / maximo * area_h
        cor = cores[i] if cores else AZUL
        partes.append(_coluna(cx - w / 2, y_base, w, h, cor))
        partes.append(f'<text x="{cx:.1f}" y="{y_base - h - 4:.1f}" text-anchor="middle" font-size="10" fill="{TEXTO_2}">{valor}</text>')
        partes.append(f'<text x="{cx:.1f}" y="{y_base + 13}" text-anchor="middle" font-size="9.5" fill="{TEXTO_2}">{escape(rotulo)}</text>')
    return _svg(largura, altura, "".join(partes), titulo)


def linhas_percentuais(rotulos_x, series, titulo, largura=340, altura=170):
    """series: [(nome, cor, [valores 0-100 ou None])]. Eixo único de 0 a 100%; valor no fim de cada linha."""
    esq, dir_, topo, base = 30, 44, 12, 24
    area_w, area_h = largura - esq - dir_, altura - topo - base
    passo = area_w / max(1, len(rotulos_x) - 1)
    y_de = lambda v: topo + area_h - v / 100 * area_h
    partes = []
    for v in (0, 50, 100):
        partes.append(f'<line x1="{esq}" y1="{y_de(v):.1f}" x2="{esq + area_w}" y2="{y_de(v):.1f}" stroke="{GRADE}" stroke-width="1"/>')
        partes.append(f'<text x="{esq - 5}" y="{y_de(v) + 3:.1f}" text-anchor="end" font-size="9" fill="{TEXTO_3}">{v}%</text>')
    for i, r in enumerate(rotulos_x):
        partes.append(f'<text x="{esq + passo * i:.1f}" y="{altura - 6}" text-anchor="middle" font-size="9.5" fill="{TEXTO_2}">{escape(r)}</text>')
    finais = []
    for _, cor, valores in series:
        pontos = [(esq + passo * i, y_de(v)) for i, v in enumerate(valores) if v is not None]
        if not pontos:
            continue
        caminho = " ".join(f"{'M' if j == 0 else 'L'}{x:.1f},{y:.1f}" for j, (x, y) in enumerate(pontos))
        partes.append(f'<path d="{caminho}" fill="none" stroke="{cor}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
        for x, y in pontos:
            partes.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{cor}" stroke="{FUNDO}" stroke-width="2"/>')
        ultimo = [v for v in valores if v is not None][-1]
        finais.append([pontos[-1][1], pontos[-1][0], ultimo])
    finais.sort()
    for i in range(1, len(finais)):
        if finais[i][0] - finais[i - 1][0] < 11:
            finais[i][0] = finais[i - 1][0] + 11
    for y, x, v in finais:
        partes.append(f'<text x="{x + 8:.1f}" y="{y + 3.5:.1f}" font-size="10" font-weight="600" fill="{TEXTO}">{v}%</text>')
    return _svg(largura, altura, "".join(partes), titulo)
