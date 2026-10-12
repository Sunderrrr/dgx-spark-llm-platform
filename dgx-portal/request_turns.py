"""Which user message(s) carry the current REQUEST.

Usually the last one. But when the model answered the request with clarifying
questions, the last user message is only the ANSWERS (« npm », « 3, portrait »)
— the request itself is in the user message before the questionnaire. Every
tool that arms on an explicit request must read both: the edit tools missed it
(files rewritten in full, 2026-10-12), then the image tool (« fais-moi 3
images pour Halloween » → questions → answers: the model wrote its
`generate_image` calls as text, 2026-10-13).
"""
import re

# A clarifying questionnaire of the model (```ask block or {"questions": [...]}).
ASKED = re.compile(r'```ask\b|"questions"\s*:\s*\[')


def request_texts(history):
    """The user texts the current request is made of: the last user message,
    plus the one before the questionnaire it answers, if any."""
    turns = [m for m in history if m.get('role') in ('user', 'assistant')]
    last = next((k for k in range(len(turns) - 1, -1, -1) if turns[k].get('role') == 'user'), None)
    if last is None:
        return []
    texts = [str(turns[last].get('content', ''))]
    if (last >= 2 and turns[last - 1].get('role') == 'assistant'
            and ASKED.search(str(turns[last - 1].get('content', '')))
            and turns[last - 2].get('role') == 'user'):
        texts.append(str(turns[last - 2].get('content', '')))
    return texts
