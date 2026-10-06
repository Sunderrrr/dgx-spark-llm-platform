# -*- coding: utf-8 -*-
"""Should the model be made to THINK about this request?

Reasoning costs tokens and time: enabling it on every message is a waste,
cutting it everywhere impoverishes the hard answers. The decision comes
from the CONTENT of the request (2026-10-03: « active le en fonction de ce
que les user demandent, pas tout le temps »).

Philosophy, deliberate: **rather under-trigger**. Thinking is expensive; a
« pourquoi… » is worth it, a « merci » is not. The markers are measured on
the real usage of the platform (infra questions, writing, code, math), not
a generic dictionary.

Standalone module (no Flask dependency): shared between the portal
(chat_routes) and, where applicable, the LiteLLM proxy.
"""

# A single one of these words makes the request « worth thinking about ».
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

# Structural signs: code or maths in the request.
MARQUEURS_CODE = ('```', 'def ', 'class ', 'function ', 'SELECT ', 'INSERT ',
                  'import ', '=>', '->', 'docker ', 'systemctl')


def raisonnablement_complexe(texte):
    """True when the request warrants paying for a reasoning pass.

    `texte` = the last user message (attachments already stripped). Rules,
    in order:
    1. explicit invitation to think → yes;
    2. code or computation present → yes;
    3. an analysis marker in the request → yes;
    4. two questions or more, or a long message → yes;
    5. otherwise → no (including greetings, thanks, « ok »).
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
    # 4. structure: several questions, or a long request
    if texte.count('?') >= 2:
        return True
    if len(texte) >= 500:
        return True
    return False
