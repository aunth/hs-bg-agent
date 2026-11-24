# Roadmap: від Advisor до Battlegrounds Agent

Архітектура `hs-bg-agent` переведена з пасивного скорера карт на повний контур **Turn-Planning Agent**:

```
State (Perception) -> Action Space & Transition Model -> Search / Policy (Value Fn + Combat Sim) -> Action Sequence -> Re-plan on observation
```

> **Примітка щодо execution-контуру (OS auto-clicker):**
> Автоматичний ввід у живий PvP-клієнт залишається **out of scope** (див. `README.md`).
> Повний closed-loop агент (разом із `act()`) реалізований у **headless-симуляторі** (`src/hsbg/env.py`, Phase 6), а в живому матчі працює **turn-planning агент** із human-in-the-loop виконанням у `overlay.py` та двомовною локалізацією (`uk` / `en`, `src/hsbg/i18n.py`).

---

## Phase 1 — Повна обсервація стану (`src/hsbg/state.py`, `src/hsbg/parser.py`)

- [x] **Hero Power state (`HeroPower`, `bg.hero_power`)**: парсинг `card_id`, актуального `COST`, статусу використання (`EXHAUSTED` / `BACON_HERO_POWER_ACTIVATED`), `passive` (`HIDE_COST`) та `playable`.
- [x] **Динамічні вартості дій (`bg.reroll_cost`, `bg.buy_cost`, `bg.upgrade_cost`)**: читання фактичних цін із тегів `TB_BaconShop_8p_Reroll_Button`, `TB_BaconShop_DragBuy`, `TB_BaconShopTechUp*`.
- [x] **Повний стан руки (`bg.hand`, `bg.hand_spells`)**: розрізнення `MINION`, `SPELL`, `BATTLEGROUND_SPELL`, фільтрація `LITERALLY_UNPLAYABLE` / `CANT_PLAY` (наприклад, заблокований `Immortal` Артаніса).
- [x] **Трекер опонентів (`OpponentBoard`, `OpponentTracker`)**:
  - [x] ідентифікація наступного суперника (`bg.next_opponent_id`, `NEXT_OPPONENT_PLAYER_ID`, `bg.combat_enemy_hero`);
  - [x] кеш останнього побаченого борду кожного суперника (`observe_combat`, `expected_opponent_board`).
- [x] **Лобі-механіки та модифікатори (`bg.trinkets`)**: активні Trinkets на локальному гравці.
- [x] **Незмінний знімок стану (`GameSnapshot`)**: серіалізований immutable dataclass (`src/hsbg/tavern.py`), придатний для копіювання в дереві пошуку без мутації `EntityStore`.

---

## Phase 2 — Модель переходів таверни (`src/hsbg/tavern.py`)

- [x] **Типізований `Action` union** (`BuyMinion`, `BuySpell`, `SellMinion`, `PlayMinion`, `PlaySpell`, `UseHeroPower`, `UpgradeTavern`, `ReorderBoard`, `FreezeShop`, `RollShop`, `EndTurn`).
- [x] **Генератор легальних дій (`legal_actions(snapshot)`)**: перевірка золота, ліміту борду (7), ліміту руки (10), ліміту тиру (6), доступності Hero Power, детекція триплету при повній руці.
- [x] **Детермінований `apply_action(snapshot, action)`**:
  - [x] базові операції (`buy / sell / play / spell / hero_power / upgrade / freeze / roll / reorder`);
  - [x] триплет-детекція (3 копії -> golden у руку + лічильник `triples_formed`);
  - [x] реєстр детермінованих Battlecry / Spell / On-Sell / Economy ефектів (`Tavern Coin`, `Hasty Excavation`, `Careful Investment`, `Blood Gem`, `Tavern Dish Banana`, `Pointy Arrow`, `Fortify`, `Alliance Flag`, `Them Apples`, `Shell Collector`, `Southsea Busker`, `Razorfen Geomancer`, `Crater Miner`, `Fearless Foodie`, `Sellemental`, `Fire Baller`, `Snow Baller`).
- [x] **Модель пулу магазину (`MinionPool`)**: розподіл тирів та активних трайбів у лобі з вагами копій по тирах для Expectimax-оцінки `RollShop()`.

---

## Phase 3 — Валідований симулятор бою (`src/hsbg/combat.py`, `tools/validate_combat.py`)

- [x] **Бенчмарк точності на реальних логах**: `tools/validate_combat.py` по всіх `data/captures/` та живій сесії — **100% exact match (8/8 боїв)**.
- [x] **Заповнений реєстр `DEATHRATTLES`** (38+ карт поточного пулу: токен-саммонери, бафери статів, щити, `Leapfrogger` рекурсія, множник `Titus Rivendare`, подвоєння для golden).
- [x] **Бойові тригери**:
  - [x] `START_OF_COMBAT` ефекти;
  - [x] `ON_ATTACK` (Rally тригери, включно з `Glim Guardian`, `Flittering Bat`, `Bonker` тощо) та `CLEAVE_CARDS`;
  - [x] динамічні бойові аури (`AURAS`: `Humming Bird`, `Murloc Warleader`, `Southsea Captain`, `Mal'Ganis`, `Raid Leader`).
- [x] **Профілювання швидкості (`CMinion.clone()`)**: досягнуто **~18 140 симуляцій/сек** на 4v4 борді (ціль була ≥5 000 симуляцій/сек).

---

## Phase 4 — Функція оцінки стану (`src/hsbg/value.py`)

- [x] **Компонент 1 — Негайна бойова еквіті (Tempo / Survival)**:
  - [x] оцінка сили столу за статами, тиром, мета-імпактом та слотами;
  - [x] інтеграція `combat.py` (`combat_bonus`, `tminion_to_cminion`, `synthetic_enemy_board`).
- [x] **Компонент 2 — Довгострокове масштабування (Scaling / Economy)**:
  - цінність рівня таверни `tier` відносно ходу (крива апу);
  - прогрес по активних білдах (`CompFit`), пари/триплети, синергія трайбів;
  - залишкові ресурси (карти в руці, касти спелів, бонус майбутнього золота, Hero Power).
- [x] **Компонент 3 — Ризик-профіль по HP/Armor**:
  - динамічний коефіцієнт greed/tier-curve та бойової терміновості залежно від `hero_health + hero_armor`.
- [x] **Декомпозиція пояснення**: `ValueBreakdown(total, board, synergy, economy, roll_ev, target_comp, combat_win_pct)`.

---

## Phase 5 — Пошуковий рушій / Turn Planner (`src/hsbg/planner.py`)

- [x] **Детермінований пошук по поточному магазину (Beam Search із transposition table)**:
  - перебір послідовностей дій без ролу до вичерпання золота або глибини `D`;
  - дедуплікація транспозицій (`state_key`);
  - бюджет часу: `<80ms` на перепланування (`p50 ~ 10ms`).
- [x] **Оптимізатор позиціювання (`optimize_positioning`)**:
  - тактичний сід (Cleave / Divine Shield / Deathrattle -> Attackers -> Support -> Taunt) + попарні перестановки під `combat.simulate` і генерація дії `ReorderBoard`.
- [x] **Стохастичні вузли (`RollShop`) — Expectimax Rollout**:
  - аналітична оцінка `roll_ev` + Expectimax семплінг магазинів із `MinionPool` (`_expectimax_roll_adjustment`) + авто-рекомендація `FreezeShop`.
- [x] **Реактивне перепланування та UI (`tools/overlay.py`, `tools/live.py`, `tools/plan_replay.py`)**:
  - покроковий `ПЛАН НА ХІД` / `TURN PLAN`, цільовий білд, альтернативна лінія (`альт`), інтерактивна панель білдів із клік-локом (`🔒`), повним списком карт (7–15 карт) та інспектором кожної карти при наведенні, двомовна локалізація (`[UA]` / `[EN]`).

---

## Phase 6 — Верифікація, Backtesting та Headless Environment

- [x] **Offline replay benchmark (`tools/eval_agent.py`)**:
  - прогін `planner.py` та `validate_combat.py` на всіх збережених і живих `Power.log`;
  - метрики: latency (`p50/p95`), % збігу з ходами гравця, симульований приріст winrate, точність симулятора бою.
- [x] **Headless Gym-середовище (`src/hsbg/env.py`)**:
  - closed-loop симулятор `BGGymEnv` (`reset / step / play_episode`) поверх `tavern.py` + `combat.py`.
- [ ] **Подальший розвиток (Next Steps)**:
  - авто-тюнінг ваг `ValueFunction` (CMA-ES / Optuna) на великій вибірці збережених логів;
  - розширення покриття специфічних Trinket/Anomaly модифікаторів та end-of-turn баферів у `tavern.py`.
