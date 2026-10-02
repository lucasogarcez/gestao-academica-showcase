import calendar
import json
from .utils import get_anos_disponiveis
from datetime import date
from django.db.models import Count, Q, F, FloatField, ExpressionWrapper
from django.db.models.functions import Cast, TruncMonth
from django.utils import timezone
from django.shortcuts import render
from django.contrib.auth.decorators import login_required, permission_required
from django.core.paginator import Paginator
from apps.academico.models import Matricula, Presenca, Chamada, Turma

@login_required
@permission_required('academico.view_fechamentomensal', raise_exception=True)
def relatorios_gerenciais(request):
    hoje = timezone.now().date()
    
    ano_str = request.GET.get('ano', '')
    mes_str = request.GET.get('mes', '')
    semana_str = request.GET.get('semana', '')
    
    # FASE 1: FILTRAGEM DE DADOS (EVENTOS)

    filtro_ano = int(ano_str) if ano_str.isdigit() else hoje.year
    # Se enviar '0' ou vazio, significa "Ano Inteiro"
    filtro_mes = int(mes_str) if mes_str.isdigit() and int(mes_str) > 0 else 0
    filtro_semana = int(semana_str) if semana_str.isdigit() else 0

    # Conta os alunos que estão com status "ATIVA" hoje nas turmas.
    retrato_atual = Matricula.objects.filter(status='ATIVA').aggregate(
        total_ativos=Count('id'),
        esporte_ativos=Count('id', filter=Q(turma_id__modalidade_id__departamento='ESPORTE')),
        lazer_ativos=Count('id', filter=Q(turma_id__modalidade_id__departamento='LAZER')),
    )

    # Conta apenas os eventos que ocorreram no Mês/Ano/Semana selecionado.
    q_tempo = Q(data_inicio__year=filtro_ano)
    if filtro_mes > 0:
        q_tempo &= Q(data_inicio__month=filtro_mes)
        
        # A semana só faz sentido matemático se estivermos dentro de um Mês
        if filtro_semana > 0:
            inicio_dia = (filtro_semana - 1) * 7 + 1
            fim_dia = filtro_semana * 7
            
            # Trava de limite do mês (fevereiro tem 28, outros 31)
            ultimo_dia_mes = calendar.monthrange(filtro_ano, filtro_mes)[1]
            if fim_dia > ultimo_dia_mes or filtro_semana == 5:
                fim_dia = ultimo_dia_mes
                
            data_inicio_semana = date(filtro_ano, filtro_mes, inicio_dia)
            data_fim_semana = date(filtro_ano, filtro_mes, fim_dia)
            
            # Filtro exato de fatiamento
            q_tempo &= Q(data_inicio__gte=data_inicio_semana, data_inicio__lte=data_fim_semana)

    fluxo_periodo = Matricula.objects.filter(q_tempo).aggregate(
        novas_matriculas=Count('id'),
        esporte_matriculas=Count('id', filter=Q(turma_id__modalidade_id__departamento='ESPORTE')),
        lazer_matriculas=Count('id', filter=Q(turma_id__modalidade_id__departamento='LAZER')),
    )

    # Junta os dois dicionários para mandar para a tela
    kpis_executivos = {**retrato_atual, **fluxo_periodo}
    
    # FASE 2: DESEMPENHO E ENGAJAMENTO (FREQUÊNCIA)
    
    q_presenca = Q(chamada_id__data_chamada__year=filtro_ano)
    
    if filtro_mes > 0:
        q_presenca &= Q(chamada_id__data_chamada__month=filtro_mes)
        # Aplica o fatiamento de semanas nas chamadas também (se selecionado)
        if filtro_semana > 0:
            q_presenca &= Q(chamada_id__data_chamada__gte=data_inicio_semana, chamada_id__data_chamada__lte=data_fim_semana)
    
    # 1. GRÁFICO: Média de Frequência por Turma (No Mês)
    freq_por_turma = Presenca.objects.filter(q_presenca).values(
        turma_id=F('chamada_id__turma_id__id'),
        modalidade=F('chamada_id__turma_id__modalidade_id__nome'),
        polo=F('chamada_id__turma_id__polo_id__nome')
    ).annotate(
        # Conta quantas presenças e atrasos a turma teve
        qtd_presencas=Count('id', filter=Q(status__in=['PRESENTE', 'ATRASO'])),
        # Conta quantas pessoas responderam a chamada
        qtd_total=Count('id')
    ).annotate(
        # Fórmula: (Presenças * 100) / Total. O Cast transforma Int em Float.
        percentual=ExpressionWrapper(
            Cast('qtd_presencas', FloatField()) * 100.0 / Cast('qtd_total', FloatField()),
            output_field=FloatField()
        )
    ).order_by('-percentual') # Melhores turmas no topo

    # 2. RADAR DE EVASÃO: Top 10 Alunos em Risco (abaixo de 70% no Mês)
    alunos_em_risco = Presenca.objects.filter(
        q_presenca,
        aluno_id__ativo=True
    ).values(
        id_aluno=F('aluno_id'),
        nome_aluno=F('aluno_id__primeiro_nome'),
        sobrenome_aluno=F('aluno_id__ultimo_nome'),
        modalidade=F('chamada_id__turma_id__modalidade_id__nome')
    ).annotate(
        qtd_presencas=Count('id', filter=Q(status__in=['PRESENTE', 'ATRASO'])),
        qtd_total=Count('id')
    ).annotate(
        percentual=ExpressionWrapper(
            Cast('qtd_presencas', FloatField()) * 100.0 / Cast('qtd_total', FloatField()),
            output_field=FloatField()
        )
    ).filter(percentual__lt=70.0).order_by('percentual')[:10] # Piores no topo (ordem crescente)

    # 3. SÉRIE TEMPORAL: Evolução Mensal da Modalidade (para o Gráfico de Linhas)
    # Filtra o Ano inteiro, agrupa por Mês e por Modalidade
    evolucao_modalidades = Presenca.objects.filter(
        chamada_id__data_chamada__year=filtro_ano
    ).annotate(
        mes_data=TruncMonth('chamada_id__data_chamada')
    ).values(
        'mes_data', 
        modalidade=F('chamada_id__turma_id__modalidade_id__nome')
    ).annotate(
        qtd_presencas=Count('id', filter=Q(status__in=['PRESENTE', 'ATRASO'])),
        qtd_total=Count('id')
    ).annotate(
        percentual=ExpressionWrapper(
            Cast('qtd_presencas', FloatField()) * 100.0 / Cast('qtd_total', FloatField()),
            output_field=FloatField()
        )
    ).order_by('mes_data')
    
    evolucao_list = []
    for item in evolucao_modalidades:
        evolucao_list.append({
            'mes': item['mes_data'].isoformat() if item['mes_data'] else None,
            'modalidade': item['modalidade'],
            'percentual': round(item['percentual'], 1) if item['percentual'] else 0
        })
    
    # FASE 3: OPERAÇÕES EQUIPE (POLOS, PROFESSORES E SECRETARIA)

    # 1. AUDITORIA DE POLOS: Quais modalidades estão ativas em cada local?
    # Busca combinações únicas de Polo e Modalidade
    polos_modalidades_raw = Turma.objects.values(
        nome_polo=F('polo_id__nome'),
        nome_modalidade=F('modalidade_id__nome')
    ).distinct().order_by('nome_polo', 'nome_modalidade')

    # Agrupa em um dicionário para facilitar a renderização HTML: {'Alpha': ['Futsal', 'Vôlei']}
    auditoria_polos = {}
    for item in polos_modalidades_raw:
        polo = item['nome_polo']
        if polo not in auditoria_polos:
            auditoria_polos[polo] = []
        auditoria_polos[polo].append(item['nome_modalidade'])
        
    lista_polos = [{'nome': p, 'modalidades': m} for p, m in auditoria_polos.items()]
    
    paginator_polos = Paginator(lista_polos, 8) # 8 cards = 2 linhas exatas no Desktop
    page_number_polos = request.GET.get('page_polos')
    auditoria_polos = paginator_polos.get_page(page_number_polos)
    
    custom_range_polos = auditoria_polos.paginator.get_elided_page_range(
        auditoria_polos.number, on_each_side=1, on_ends=1
    )
        
    # Filtro de tempo específico para a Tabela de Chamadas
    q_chamada = Q(data_chamada__year=filtro_ano)
    if filtro_mes > 0:
        q_chamada &= Q(data_chamada__month=filtro_mes)
        if filtro_semana > 0:
            q_chamada &= Q(data_chamada__gte=data_inicio_semana, data_chamada__lte=data_fim_semana)

    # 2. PRODUTIVIDADE DE PROFESSORES: Quantas aulas cada um deu no mês?
    produtividade_prof = Chamada.objects.filter(q_chamada).values(
        nome_prof=F('turma_id__professores__usuario__username')
    ).annotate(
        aulas_dadas=Count('id')
    ).order_by('-aulas_dadas')

    # 3. MÉTRICAS DA SECRETARIA: Matrículas por Atendente no mês
    matriculas_secretaria_raw = Matricula.objects.filter(q_tempo).values(
        atendente=F('realizado_por__first_name')
    ).annotate(
        total=Count('id')
    ).order_by('-total')

    # Calcula a Média de Matrículas
    lista_matriculas_sec = list(matriculas_secretaria_raw)
    media_matriculas_sec = 0
    if lista_matriculas_sec:
        total_mat = sum(item['total'] for item in lista_matriculas_sec)
        media_matriculas_sec = round(total_mat / len(lista_matriculas_sec), 1)
        
    context = {
        'filtro_mes': filtro_mes,
        'filtro_ano': filtro_ano,
        'filtro_semana': str(filtro_semana) if filtro_semana else '',
        'active_dashboard_tab': request.GET.get('tab', 'visao_geral'),
        'anos_disponiveis': get_anos_disponiveis(),
        'lista_meses': [(1, 'Janeiro'), (2, 'Fevereiro'), (3, 'Março'), (4, 'Abril'),
            (5, 'Maio'), (6, 'Junho'), (7, 'Julho'), (8, 'Agosto'),
            (9, 'Setembro'), (10, 'Outubro'), (11, 'Novembro'), (12, 'Dezembro')
        ],
            
        # Fase 1 (Visão Executiva)
        'kpis_executivos': kpis_executivos,
            
        # Fase 2 (Saúde das Turmas)
        'freq_por_turma': freq_por_turma,
        'alunos_em_risco': alunos_em_risco,
        'evolucao_modalidades_json': json.dumps(evolucao_list), 
            
        # Fase 3 (Operações e Equipe)
        'auditoria_polos': auditoria_polos,
        'custom_range_polos': custom_range_polos,
        'produtividade_prof': produtividade_prof,
        'matriculas_secretaria_json': json.dumps(lista_matriculas_sec),
        'media_matriculas_sec': media_matriculas_sec,
    }

    return render(request, 'relatorio_gerencial.html', context)