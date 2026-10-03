# -*- coding: utf-8 -*-
"""Faut-il faire RÉFLÉCHIR le modèle pour cette demande ?

Le raisonnement coûte des tokens et du temps : l'activer à chaque message est
un gaspillage, le couper partout appauvrit les réponses difficiles. Décision
donnée par le CONTENU de la demande (2026-10-03 : « active le en fonction de
ce que les user demandent, pas tout le temps »).

Philosophie, assumée : **plutôt sous-déclencher**. Le thinking est cher ; un
« pourquoi… » en vaut la peine, un « merci » non. Les marqueurs sont mesurés
sur l'usage réel de la plateforme (questions d'infra, rédaction, code,
calculs), pas un dictionnaire générique.

Module autonome (aucune dépendance Flask) : partagé entre le portail
(chat_routes) et, le cas échéant, le proxy LiteLLM.
"""

# Un seul de ces mots rend la demande « digne de réflexion ».
MARQUEURS = (
    # raisonnement explicite
    'pourquoi', 'comment faire', 'comment puis-je', 'explique', 'expliquer',
    'démontre', 'prouve', 'raisonne', 'réfléchis', 'étape par étape',
    'step by step', 'pas à pas',
    # travail d'analyse
    'compare', 'comparer', 'analyse', 'analyser', 'évalue', 'évaluer',
    'optimise', 'optimiser', 'arbitrage', 'trade-off', 'avantages',
    'inconvénients', 'risques', 'meilleur', 'meilleure', 'que choisir',
    'lesquels', 'laquelle', 'vaut-il', 'faut-il',
    # technique / code / calcul
    'corrige', 'corriger', 'debug', 'débogue', 'erreur', 'traceback',
    'stacktrace', 'bug', 'algorithme', 'complexité', 'sql', 'script',
    'fonction', 'requête', 'architecture', 'migration', 'sécurité', 'audit',
    'calcule', 'calculer', 'équation', 'probabilité', 'statistique',
    # planification
    'stratégie', 'plan', 'planifier', 'organise', 'étapes', 'procédure',
)

# Signes structurels : du code ou des maths dans la demande.
MARQUEURS_CODE = ('```', 'def ', 'class ', 'function ', 'SELECT ', 'INSERT ',
                  'import ', '=>', '->', 'docker ', 'systemctl')


def raisonnablement_complexe(texte):
    """True when the request warrants paying for a reasoning pass.

    `texte` = le dernier message utilisateur (déjà dépouillé des pièces
    jointes). Règles, dans l'ordre :
    1. invitation explicite à réfléchir → oui ;
    2. code ou calcul présent → oui ;
    3. un marqueur d'analyse dans la demande → oui ;
    4. deux questions ou plus, ou un message long → oui ;
    5. sinon → non (y compris les salutations, remerciements, « ok »).
    """
    if not texte or not texte.strip():
        return False
    bas = texte.lower()
    # 1. demande explicite
    if any(m in bas for m in ('réfléchis', 'raisonne', 'étape par étape',
                              'step by step', 'pas à pas', 'réfléchir')):
        return True
    # 2. code / maths
    if any(m in texte for m in MARQUEURS_CODE):
        return True
    if sum(ch.isdigit() for ch in texte) >= 4 and any(
            op in texte for op in ('+', '-', '*', '/', '=', '%')):
        return True
    # 3. marqueur d'analyse
    if any(m in bas for m in MARQUEURS):
        return True
    # 4. structure : plusieurs questions, ou demande longue
    if texte.count('?') >= 2:
        return True
    if len(texte) >= 500:
        return True
    return False
