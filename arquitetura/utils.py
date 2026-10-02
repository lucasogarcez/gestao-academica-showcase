import difflib
import pandas as pd
import unicodedata
from django.db.models import Value, Q, CharField
from .models import Situacao, Aluno, DiaSemana, Matricula
from apps.pessoas.models import Professor, Estagiario
from django.db.models.functions import Concat
import logging

logger = logging.getLogger('gestoredu')

"""
MÓDULO DE INTEGRAÇÃO E DATA CLEANSING (ETL)
Responsável por sanitizar e importar dados não-estruturados de planilhas Excel externas.
Destaques de Engenharia:
- Sobrevive a quebras de layout e células mescladas (Parser Dinâmico).
- Utiliza difflib (Fuzzy Matching) para auto-correção de erros de digitação em nomes de professores.
- Aplica Fail-Fast e Intertravamento: recusa a transação inteira se detectar anomalias nos dados relacionais.
"""

def remover_acentos(txt):
    """ Remove acentos, espaços e caracteres especiais (Ex: 'Natação' -> 'NATACAO') """
    if not txt: return ""
    txt = str(txt).strip().upper()
    return ''.join(c for c in unicodedata.normalize('NFD', txt) if unicodedata.category(c) != 'Mn')

def get_dias_canonicos(str_dias):
    """
    Retorna 3 valores: (String Formatada, Lista de Enums, Quantidade de Dias)
    """
    
    if not str_dias or pd.isna(str_dias):
        # Retorna 3 valores para não quebrar o unpack
        return "A DEFINIR", [], 0

    s = str(str_dias).upper()
    
    # Detecção
    mapa_dias = {
        DiaSemana.DOMINGO: ["DOM"],
        DiaSemana.SEGUNDA: ["SEG", "2"],
        DiaSemana.TERCA: ["TER", "3"],
        DiaSemana.QUARTA: ["QUA", "4"],
        DiaSemana.QUINTA: ["QUI", "5"],
        DiaSemana.SEXTA: ["SEX", "6"],
        DiaSemana.SABADO: ["SAB", "SÁB"],
    }
    
    dias_enums = {
        dia
        for dia, chaves in mapa_dias.items()
        if any(chave in s for chave in chaves)
    }
    
    if not dias_enums:
        return str_dias, [], 0

    # Ordenação Semanal (Dom -> Sab)
    ordem = {dia: i for i, dia in enumerate(mapa_dias.keys())}
    
    lista_ordenada = sorted(dias_enums, key=lambda d: ordem[d])
    labels = [d.label.upper() for d in lista_ordenada]
    
    # IMPORTANTE: Retorna 3 valores
    return ", ".join(labels), lista_ordenada, len(lista_ordenada)

def normalizar_merges(ws):
    """
    Garante que o valor de um merged range esteja
    na célula superior esquerda.
    """
    for merge in ws.merged_cells.ranges:
        # Célula-mãe
        top_left = ws.cell(row=merge.min_row, column=merge.min_col)

        if top_left.value:
            continue

        # Procura valor em qualquer célula do merge
        for row in ws.iter_rows(
            min_row=merge.min_row,
            max_row=merge.max_row,
            min_col=merge.min_col,
            max_col=merge.max_col
        ):
            for cell in row:
                if cell.value:
                    top_left.value = cell.value
                    cell.value = None
                    break
            if top_left.value:
                break

def ler_ficha_chamada(ws):
    """
    Parser Dinâmico de Duas Fases para Planilhas do Excel.
    Sobrevive a alterações de layout, colunas inseridas e rodapés poluídos.
    """
    dados = {
        'modalidade': None, 'local': None, 'horario': None, 
        'dias': None, 'professor': None, 'estagiario': None,
        'categoria': None, 'alunos': []
    }
    
    # SENSOR DE BUSCA HORIZONTAL (Ignora buracos de Merges)
    def extrair_valor_a_direita(linha, col_inicial):
        """ Varre até 5 colunas para a direita procurando o primeiro valor não-vazio """
        for col in range(col_inicial + 1, col_inicial + 6):
            valor = ws.cell(row=linha, column=col).value
            if valor is not None and str(valor).strip() != "":
                return valor
        return None

    # EXTRAÇÃO DE METADADOS (CABEÇALHO ESTÁTICO DO TOPO)
    for row in ws.iter_rows(min_row=1, max_row=15, min_col=1, max_col=25):
        for cell in row:
            valor_celula = str(cell.value).strip().lower() if cell.value else ""
            
            # Usamos o nosso novo sensor para capturar o valor!
            if "modalidade" in valor_celula and not dados['modalidade']:
                dados['modalidade'] = extrair_valor_a_direita(cell.row, cell.column)
            elif "local" in valor_celula and not dados['local']:
                dados['local'] = extrair_valor_a_direita(cell.row, cell.column)
            elif ("horário" in valor_celula or "horario" in valor_celula) and not dados['horario']:
                dados['horario'] = extrair_valor_a_direita(cell.row, cell.column)
            elif "professor" in valor_celula and not dados['professor']:
                dados['professor'] = extrair_valor_a_direita(cell.row, cell.column)
            elif ("estagiário" in valor_celula or "estagiario" in valor_celula) and not dados['estagiario']:
                dados['estagiario'] = extrair_valor_a_direita(cell.row, cell.column)
            elif "categoria" in valor_celula and not dados['categoria']:
                dados['categoria'] = extrair_valor_a_direita(cell.row, cell.column)
            elif "dias" in valor_celula and not dados['dias']:
                texto_original = str(cell.value).strip()
                if len(texto_original) > 6:
                    dados['dias'] = texto_original
                else:
                    dados['dias'] = extrair_valor_a_direita(cell.row, cell.column)

    # FASE 1: CALIBRAÇÃO / MAPEAMENTO DINÂMICO (TABELA DE ALUNOS)
    mapa_colunas = {}
    linha_cabecalho_idx = None
    
    # Dicionário de Sinônimos (Sensibilidade a Variações do Excel)
    termos_num = ['nº', 'n', 'numero', 'número', 'seq', 'ordem']
    termos_nome = ['nome', 'aluno', 'nome do aluno', 'estudante']
    termos_nasc = ['nascimento', 'data nasc', 'dt nasc', 'idade', 'data de nascimento', 'data nascimento']
    termos_atestado = ['atestado', 'validade', 'vencimento', 'atestado médico']

    # Varre as primeiras 30 linhas procurando onde a tabela de alunos começa
    for row in ws.iter_rows(min_row=1, max_row=30):
        mapa_temp = {}
        for cell in row:
            valor = str(cell.value).strip().lower() if cell.value else ""
            if not valor:
                continue
            
            # Identificação por similaridade de String
            if any(t == valor or valor.startswith(t) for t in termos_num) and 'numero' not in mapa_temp:
                mapa_temp['numero'] = cell.column
            elif any(t in valor for t in termos_nome) and 'nome' not in mapa_temp:
                mapa_temp['nome'] = cell.column
            elif any(t in valor for t in termos_nasc) and 'nascimento' not in mapa_temp:
                mapa_temp['nascimento'] = cell.column
            elif any(t in valor for t in termos_atestado) and 'atestado' not in mapa_temp:
                mapa_temp['atestado'] = cell.column

        # Se achou as colunas Número e Nome, marca a linha zero da tabela
        if 'numero' in mapa_temp and 'nome' in mapa_temp:
            mapa_colunas = mapa_temp
            linha_cabecalho_idx = row[0].row
            logger.info(f"[Scanner Excel] Cabeçalho da tabela localizado na linha {linha_cabecalho_idx}. Mapeamento: {mapa_colunas}")
            break

    # FASE 2: FILTRO POR SYNC WORD E TIPAGEM (EXTRAÇÃO DE ALUNOS)
    if linha_cabecalho_idx and 'numero' in mapa_colunas:
        col_num = mapa_colunas.get('numero')
        col_nome = mapa_colunas.get('nome')
        col_nasc = mapa_colunas.get('nascimento')
        col_atest = mapa_colunas.get('atestado')

        # Inicia varredura das linhas exatamente abaixo do cabeçalho mapeado
        for row_idx in range(linha_cabecalho_idx + 1, ws.max_row + 1):
            
            # Acesso direto à célula para que a sua função normalizar_merges() faça efeito
            celula_num = ws.cell(row=row_idx, column=col_num).value
            
            # Tenta converter a coluna "Nº" para número real
            try:
                # Usamos float() dentro de int() caso o Excel mande "1.0"
                num_aluno = int(float(str(celula_num).strip().replace(',', '.')))
            except (ValueError, TypeError):
                # Não é um número inteiro válido. 
                # A tabela acabou ou é uma linha de lixo. Pula para a próxima
                continue
            
            # Se passou do bloco try, é uma linha de aluno
            # Captura os dados dinamicamente usando as colunas mapeadas na FASE 1
            nome = ws.cell(row=row_idx, column=col_nome).value if col_nome else None
            nascimento = ws.cell(row=row_idx, column=col_nasc).value if col_nasc else None
            atestado = ws.cell(row=row_idx, column=col_atest).value if col_atest else None
            
            dados['alunos'].append({
                'nome': nome,
                'nascimento': nascimento,
                'atestado': atestado
            })
    else:
        logger.warning("[Scanner Excel] Erro fatal: Tabela de alunos (Colunas Nº e Nome) não encontrada no documento.")

    return dados

def calcular_status_aluno(nome_completo, turma_id=None):
    """
    Retorna o status do aluno com base no banco:
    NOVO | EXISTENTE | ADD_TURMA | MATRICULADO
    """
    # Relé de Proteção contra vácuo: Desarma se o nome não existir
    if not nome_completo or not str(nome_completo).strip():
        return "NÃO IDENTIFICADO", None
    
    parts = str(nome_completo).strip().split()
    p_nome = parts[0]
    u_nome = ' '.join(parts[1:]) if len(parts) > 1 else ''

    aluno = Aluno.objects.filter(
        primeiro_nome__iexact=p_nome,
        ultimo_nome__iexact=u_nome
    ).first()

    if not aluno:
        return 'NOVO', None

    if turma_id:
        if Matricula.objects.filter(
            aluno_id=aluno.id,
            turma_id=turma_id,
            status=Situacao.ATIVA
        ).exists():
            return 'MATRICULADO', aluno
        else:
            return 'ADD_TURMA', aluno

    return 'EXISTENTE', aluno

def rastrear_placas_docentes(valor_celula: str, tipo_componente='professor'):
    """
    Sensor de Varredura Multicanal com Intertravamento Rigoroso:
    Se QUALQUER componente listado na string não for encontrado no banco de dados,
    dispara um curto-circuito (ValueError) abortando toda a importação.
    """
    resultados = []
    
    if not valor_celula or str(valor_celula).strip() in ["", "Não Informado", "-", "None"]:
        return resultados

    # Suporta tanto '/' quanto quebras de linha '\n'
    string_normalizada = str(valor_celula).replace('\n', '/')
    canais_extraidos = [nome.strip() for nome in string_normalizada.split('/') if nome.strip()]
    
    if not canais_extraidos:
        return resultados

    ModeloFisico = Professor if tipo_componente == 'professor' else Estagiario

    for nome_bruto in canais_extraidos:
        nome_limpo = " ".join(nome_bruto.split())
        partes = nome_limpo.split()
        
        if not partes:
            continue
            
        componente_encontrado = None

        # Estágio 1: Encaixe Perfeito
        componente_encontrado = ModeloFisico.objects.annotate(
            nome_completo=Concat('usuario__first_name', Value(' '), 'usuario__last_name', output_field=CharField())
        ).filter(nome_completo__iexact=nome_limpo).first()

        # Estágio 2: Filtro de Extremos
        if not componente_encontrado and len(partes) >= 2:
            componente_encontrado = ModeloFisico.objects.filter(
                usuario__first_name__icontains=partes[0],
                usuario__last_name__icontains=partes[-1]
            ).first()

        # Estágio 3: Sensor de Baixa Resolução
        if not componente_encontrado:
            componente_encontrado = ModeloFisico.objects.filter(
                Q(usuario__first_name__iexact=partes[0]) | Q(usuario__username__iexact=partes[0])
            ).first()

        # DECISÃO DO MÓDULO LOGICO (Relé de Intertravamento)
        if componente_encontrado:
            resultados.append(componente_encontrado)
        else:
            todos_docentes = ModeloFisico.objects.annotate(
                nome_completo=Concat('usuario__first_name', Value(' '), 'usuario__last_name', output_field=CharField())
            ).values_list('nome_completo', flat=True)
            
            # Procura a melhor correspondência com 45% de margem de acerto
            sugestoes = difflib.get_close_matches(nome_limpo, list(todos_docentes), n=1, cutoff=0.45)
            
            titulo = "Professor(a)" if tipo_componente == 'professor' else "Estagiário(a)"
            
            if sugestoes:
                msg_erro = f"Erro na planilha: {titulo} '{nome_limpo}' não encontrado(a). Você quis dizer '{sugestoes[0]}'?"
            else:
                msg_erro = f"Erro na planilha: {titulo} '{nome_limpo}' não existe no sistema. Cadastre-o(a) primeiro."
                
            logger.error(msg_erro)
            raise ValueError(msg_erro)
            
    return resultados
