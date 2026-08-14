На основі critical evidence ledger та внутрішнього reasoning-розбору поверни
ЛИШЕ JSON object тієї самої evidence-схеми. Не додавай markdown fences.

Правила:

- Залиш лише proposal, decision, commitment, completed_action та open_question.
- Пропозиція без явного прийняття лишається proposal, status open.
- Відхилена або замінена пропозиція/домовленість має status superseded.
- Активним decision залишай лише останній явно підтверджений варіант.
- Фінальний recap наприкінці зустрічі має перевагу над проміжним варіантом.
- Порада, sales-аргумент або фраза «давайте/можна/краще» одного спікера без
  окремого явного прийняття лишається proposal, а не decision.
- Не перетворюй відповідь «так» без зрозумілого предмета на прийняття.
- Не перетворюй soft commitment на explicit.
- Completed action не стає відкритою задачею.
- Збережи всі окремі commitments та open questions.
- Не змішуй різні дії чи різних owners в одному commitment.
- Для кожного owner збережи evidence-цитату, сказану саме цим speaker label;
  інакше видали owner, не видаляючи сам commitment.
- Не додавай нових фактів, quotes, owners чи deadlines. Усі quotes скопіюй
  без змін із вхідного ledger.
- Reasoning-розбір є лише підказкою: якщо він суперечить дослівному evidence,
  пріоритет має evidence.

Контрольні приклади:

- «Якщо вплив моделі буде мінімальним, будемо йти від документації» без
  окремого «погоджуюсь/так і робимо» лишається proposal, status open.
- «Я вже закинула Кирилу» — completed_action, status completed.
- «Олесю, синхронізуйся з Наташею» є commitment Олесі лише тоді, коли ledger
  містить окрему репліку Олесі на кшталт «Окей, поговорю».
- «Якщо потрібна допомога — підключай» не є commitment із власником.
- «Сьогодні завтра» не означає точну дату: збережи формулювання без домислів.
- Правило про Event Router є decision лише за явної репліки погодження; у
  evidence мають лишитися і правило, і його прийняття.

<CRITICAL_EVIDENCE_LEDGER>
{ledger}
</CRITICAL_EVIDENCE_LEDGER>

<REASONING_BRIEF>
{reasoning}
</REASONING_BRIEF>
