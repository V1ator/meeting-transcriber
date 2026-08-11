Об'єднай хронологічно впорядковані critical evidence ledgers у один і поверни
ЛИШЕ JSON object тієї самої схеми.

- Залиш proposal, decision, commitment, completed_action та open_question.
- Прибери лише справжні дублікати; не зливай різні кроки одного процесу.
- Збережи всі змістовні operational items без загального ліміту.
- Збережи цитати прийняття, заперечення, скасування та завершення.
- На цьому кроці не вгадуй фінальний статус конфліктних пунктів: якщо є сумнів,
  збережи обидві позиції для наступного reconciliation-проходу.
- Не вигадуй цитат, owners або deadlines.
- Не перенось owner з чужої репліки: owner повинен мати власну evidence-цитату
  прийняття з тим самим speaker label.

<EVIDENCE_LEDGERS>
{ledgers}
</EVIDENCE_LEDGERS>
