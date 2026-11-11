# Персоны и агент-пользователь (T2)

Персоны — `scripts/sim/personas/*.json` (коммитятся, без ПДн). Загрузчик T1 (`harness.Persona.load`) берёт из файла
`mode`, `task`, `voice`, `confirm_card`, `env`, остальные поля игнорирует: их читают агент-пользователь
(`answer_user.py`) и судья (`docs/v3/SIM_RUBRIC.md`). Подсчёт — `score.py`.

## Формат персоны

| Поле | Что |
|---|---|
| `name` | = имя файла; `id` — `G1`…`G9` (золотой набор `docs/v2/GOLDEN_SET.md`) или `N<n>` (новые) |
| `role` | `buyer_home`, `buyer_land`, `renter` (объекты); `investor_coinvest`, `agent_buyers`, `post_link` (лиды); `both` |
| `language` | язык первого сообщения: `es`, `ru`, `en`, `mixed`; `unknown` — пост владельца |
| `mode`, `task`, `voice`, `confirm_card`, `env` | формат T1 (`scripts/sim/README.md`, «Прогон») |
| `task_source` | `text` — задача в `task`; `owner_O7` — ссылку на пост даёт владелец, `task` пуст |
| `fixtures` | есть ли объекты/лиды в записанных фикстурах или что нужно записать (для «нет данных» в рубрике) |
| `replies` | слот → варианты ответа `{"text": …}` или `{"press": "<текст или data кнопки>"}`, `weight`, `tag` |
| `truth` | что считается точной находкой (`objects`) и разумным лидом (`leads`), см. рубрику §1–2 |

Теги вариантов: `any` — «не знаю / не важно / любой»; `contradiction` — противоречит сказанному раньше (бот должен
переспросить); `low_budget` — правдоподобный, но заниженный бюджет (не противоречие: критерий проверяется по нему);
`edit` — правка на карточке «Проверьте задачу», не меняет жёсткие критерии `truth`. Без тега — прямой ответ,
совпадающий с `truth`.

Критерий `truth` с `"ask": "required"` — бот обязан спросить этот слот; не спросил — судья проверяет по `value`
(`docs/v3/SIM_RUBRIC.md` §1).

Слоты (`answer_user.SLOT_PATTERNS`, первый совпавший): `clarify` (бот заметил противоречие), `deviation`, `details`
(общий вопрос: стадия, под какое строительство, этаж/лифт), `investor_role`, `investor_who`, `ticket`, `term`,
`budget`, `property_type`, `area`, `rooms`, `deal`, `place`, `district`, `must_have`, `sources`; `card` — карточка
(`card: true` в pending). Свои шаблоны персоны — `slot_patterns: {"<slot>": ["<regex>"]}` (проверяются первыми).

Персоны G6 и G8 — пост по ссылке: ссылку даёт владелец (O7). До неё прогон не запускается; оркестратор кладёт копию с
`task` = ссылка в `sim/personas/` (в `.gitignore`), в git ссылка не попадает. `score.py` всё равно ждёт роль
`post_link` в каждой партии: без поста цель не достигнута.

## Протокол агента-пользователя

1. `python -m scripts.sim.run --persona scripts/sim/personas/<p>.json --run <batch>-<p>` вернул `3` и
   `pending answers: 1`.
2. `python -m scripts.sim.answer_user --run <batch>-<p> --persona scripts/sim/personas/<p>.json --seed <N>`:
   - читает `sim/pending_answers/<run>.json` (`index`, `card`, `question`, `buttons`);
   - слот вопроса — по тексту после «Понял: …»; карточка — слот `card`;
   - из `replies[slot]` убираются нажатия кнопок, которых нет на экране (на карточке «Запустить» есть всегда);
   - если этот слот уже спрашивали (журнал `log` в `sim/answers/<run>.json`), варианты `any`, `contradiction`,
     `low_budget`, `edit` убираются: на повторный вопрос пользователь отвечает прямо;
   - выбор — `random.Random(f"{seed}:{run}:{index}:{slot}").choices(варианты, weights)`: тот же seed — тот же
     диалог;
   - ответ дописывается в `answers`, строка `{index, slot, tag, seed}` — в `log`; pending удаляется. Код `0`.
3. Код `2` — вопрос оставлен: слот не распознан или у персоны нет ответа на него. Агент-пользователь (Claude в
   роли персоны) отвечает сам, в рамках роли и `truth`, **только через помощник**:
   `python -m scripts.sim.answer_user --run <id> --persona <file> --seed <N> --manual "<текст>"` (или
   `--manual-press "<кнопка>"`). Помощник дописывает ответ и обязательную строку `log` с `tag: "manual"` и удаляет
   pending; руками `answers/<run>.json` не правят. Такой вопрос стоит добавить в персону.
4. Код `1` — отвечать нечего (нет pending или он уже отвечен). Перезапустить `run`.

Seed партии — один на все прогоны партии, записывается в строку оценки (`seed`); у партий в серии «3 подряд» он
разный. Подсчёт: `python -m scripts.sim.score --check` (до ссылок O7 — `--exclude G6,G8`, цель тогда «условно»;
исключать можно только персоны с `task_source: owner_O7`). В партии одна строка на персону; `--latest` — взять
последнюю, если персону разметили дважды. Пользователь выбирает случайно,
но в рамках роли: `contradiction` — малый вес, после него следует прямой ответ на уточнение.
