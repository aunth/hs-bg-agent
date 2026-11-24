"""Localization (i18n) support for hs-bg-agent (Ukrainian `uk` and English `en`).

Language selection priority:
1. Explicit `set_lang("uk" | "en")` call (or clicking `[UA|EN]` in the overlay).
2. `HSBG_LANG` environment variable (`uk` or `en`).
3. Default: `"uk"` (Ukrainian).
"""
from __future__ import annotations

import os
import re

SUPPORTED_LANGS = ("uk", "en")
_CURRENT_LANG = os.environ.get("HSBG_LANG", "uk").lower().strip()
if _CURRENT_LANG not in SUPPORTED_LANGS:
    _CURRENT_LANG = "uk"


def get_lang() -> str:
    return _CURRENT_LANG


def set_lang(lang: str) -> str:
    global _CURRENT_LANG
    norm = lang.lower().strip()
    if norm in ("ua", "uk", "ukr"):
        _CURRENT_LANG = "uk"
    elif norm in ("en", "eng"):
        _CURRENT_LANG = "en"
    return _CURRENT_LANG


def toggle_lang() -> str:
    return set_lang("en" if _CURRENT_LANG == "uk" else "uk")


TRIBE_NAMES: dict[str, dict[str, str]] = {
    "uk": {
        "BEAST": "Звір",
        "BEASTS": "Звірі",
        "DEMON": "Демон",
        "DEMONS": "Демони",
        "DRAGON": "Дракон",
        "DRAGONS": "Дракони",
        "ELEMENTAL": "Елементаль",
        "ELEMENTALS": "Елементалі",
        "MECHANICAL": "Мех",
        "MECH": "Мех",
        "MECHS": "Мехи",
        "MURLOC": "Мурлок",
        "MURLOCS": "Мурлоки",
        "NAGA": "Нага",
        "NAGAS": "Наги",
        "PIRATE": "Пірат",
        "PIRATES": "Пірати",
        "QUILBOAR": "Свинобраз",
        "QUILBOARS": "Свинобрази",
        "UNDEAD": "Нежить",
        "ALL": "Усі",
        "NEUTRAL": "Нейтрал",
    },
    "en": {
        "BEAST": "Beast",
        "DEMON": "Demon",
        "DRAGON": "Dragon",
        "ELEMENTAL": "Elemental",
        "MECHANICAL": "Mech",
        "MECH": "Mech",
        "MURLOC": "Murloc",
        "NAGA": "Naga",
        "PIRATE": "Pirate",
        "QUILBOAR": "Quilboar",
        "UNDEAD": "Undead",
        "ALL": "All",
        "NEUTRAL": "Neutral",
    },
}

ROLE_NAMES: dict[str, dict[str, str]] = {
    "uk": {
        "CORE": "Ядро",
        "ADDON": "Доп",
        "CYCLE": "Цикл",
        "HAVE": "Є",
    },
    "en": {
        "CORE": "Core",
        "ADDON": "Addon",
        "CYCLE": "Cycle",
        "HAVE": "HAVE",
    },
}

STRINGS: dict[str, dict[str, str]] = {
    "uk": {
        "app_title": "ПОЛЯ БИТВ",
        "live": "● live",
        "idle": "○ очікування",
        "waiting_session": "очікування ігрової сесії...",
        "waiting_match": "очікування матчу…",
        "no_power_log": "Power.log ще не створено.",
        "start_hs_hint": "Запусти Hearthstone і зайди в матч Battlegrounds.",
        "no_live_data": "НЕМАЄ ЖИВИХ ДАНИХ",
        "last_log_write": "  останній запис {ago} тому",
        "no_log_yet": "  логу ще немає",
        "stale_line_1": "  стіл нижче — з попереднього стану,",
        "stale_line_2": "  а не поточний хід.",
        "stale_line_3": "  якщо ти в матчі, Hearthstone",
        "stale_line_4": "  зупинив запис логу —",
        "stale_line_5": "  перезапусти клієнт.",
        "turn_stat": "хід {turn}   рів {tier}   зол {gold}{up}",
        "up_cost": "  ап {cost}з",
        "last_action": "остання дія:",
        "combat_odds": "БІЙ  перем {win:.0f}%  нічия {tie:.0f}%  пор {loss:.0f}%",
        "hero_pick": "ВИБІР ГЕРОЯ",
        "discover": "РОЗКОПКА",
        "choose_n": "{kind} — обери {n}",
        "hero_stats": "     сер #{avg:.2f}   пік {pick:.0f}%   {games} ігор",
        "no_stats": "     немає статистики",
        "turn_plan": "ПЛАН НА ХІД",
        "target_label": "ціль: {name} ({power})",
        "alt_prefix": "  альт: {text}",
        "hold_turn": "Утриматись / Кінець ходу",
        "shop_header": "МАГАЗИН ({n})",
        "enemy_board_header": "СТІЛ ВОРОГА ({n})",
        "spells_header": "ЗАКЛИНАННЯ ({n})",
        "board_header": "ТВІЙ СТІЛ ({n})",
        "hand_header": "РУКА ({n})",
        "empty": "  (порожньо)",
        "building_toward": "▼ ЦІЛЬОВІ БІЛДИ",
        "building_toward_show": "▶ ЦІЛЬОВІ БІЛДИ",
        "pivot_check": "ПЕРЕВІРКА ПІВОТУ",
        "builds_lobby_title": "▼ БІЛДИ ДЛЯ ЦЬОГО ЛОБІ",
        "builds_collapsed_pill": "▶ БІЛДИ",
        "locked_badge": "🔒",
        "duos_warn": "DUOS — соло-BG стат.",
        "builds_hint_header": "",
        "tribes_prefix": "племена: ",
        "learning_tag": "+аналіз",
        "no_viable_build": "ще немає визначеного білду",
        "cards_count": "[{n} карт]",
        "point_at_build": "наведи на білд (або клікни для локу), щоб побачити всі карти",
        "no_card_text": "Немає тексту карти.",
        "spell_or_token": "Заклинання/Токен",
        "inspector_locked_hint": "білд ЗАФІКСОВАНО — наведи на будь-яку карту вище для опису (клік на білд — розлок)",
        "inspector_ unlocked_hint": "наведи на будь-яку карту вище для повного опису · клік на рядок білду — фіксація",
        # Actions
        "act_buy_minion": "Купити {name} (-{cost}з)",
        "act_buy_spell": "Купити спел {name} (-{cost}з)",
        "act_sell_minion": "Продати {name} (+{gain}з)",
        "act_play_minion": "Виставити {name}",
        "act_play_spell": "Зіграти {name}{cost_str}",
        "act_hero_power": "Сила героя: {name} (-{cost}з)",
        "act_upgrade": "Ап до T{tier} (-{cost}з)",
        "act_reorder": "Порядок: {order}",
        "act_freeze_targets": "Заморозити ({targets})",
        "act_freeze": "Заморозити магазин",
        "act_roll": "Рол (-{cost}з) -> переплан",
        "act_end_turn": "Кінець ходу",
        "badge_upgrade": "▲ АП",
        "badge_reroll": "↻ РОЛ",
    },
    "en": {
        "app_title": "BATTLEGROUNDS",
        "live": "● live",
        "idle": "○ waiting",
        "waiting_session": "waiting for a game session...",
        "waiting_match": "waiting for a match…",
        "no_power_log": "No Power.log yet.",
        "start_hs_hint": "Start Hearthstone and enter a Battlegrounds match.",
        "no_live_data": "NO LIVE DATA",
        "last_log_write": "  last log write {ago} ago",
        "no_log_yet": "  no log yet",
        "stale_line_1": "  board below is the last game,",
        "stale_line_2": "  not the current one.",
        "stale_line_3": "  if you are mid-game, Hearthstone",
        "stale_line_4": "  has stopped writing logs —",
        "stale_line_5": "  restart the client.",
        "turn_stat": "turn {turn}   tier {tier}   gold {gold}{up}",
        "up_cost": "  up {cost}g",
        "last_action": "last action:",
        "combat_odds": "COMBAT  win {win:.0f}%  tie {tie:.0f}%  loss {loss:.0f}%",
        "hero_pick": "HERO PICK",
        "discover": "DISCOVER",
        "choose_n": "{kind} — choose {n}",
        "hero_stats": "     avg {avg:.2f}   picked {pick:.0f}%   {games} games",
        "no_stats": "     no stats",
        "turn_plan": "TURN PLAN",
        "target_label": "target: {name} ({power})",
        "alt_prefix": "  alt: {text}",
        "hold_turn": "Hold / End turn",
        "shop_header": "SHOP ({n})",
        "enemy_board_header": "ENEMY BOARD ({n})",
        "spells_header": "SPELLS ({n})",
        "board_header": "BOARD ({n})",
        "hand_header": "HAND ({n})",
        "empty": "  (empty)",
        "building_toward": "▼ BUILDING TOWARD",
        "building_toward_show": "▶ BUILDING TOWARD",
        "pivot_check": "PIVOT CHECK",
        "builds_lobby_title": "▼ BUILDS FOR THIS LOBBY",
        "builds_collapsed_pill": "▶ BUILDS",
        "locked_badge": "🔒",
        "duos_warn": "DUOS — solo-BG data",
        "builds_hint_header": "",
        "tribes_prefix": "tribes: ",
        "learning_tag": "+learning",
        "no_viable_build": "no viable build yet",
        "cards_count": "[{n} cards]",
        "point_at_build": "point at a build (or click to lock) to see all its cards",
        "no_card_text": "No card text.",
        "spell_or_token": "Spell/Token",
        "inspector_locked_hint": "build LOCKED — hover any card above to inspect its text & stats (click build row to unlock)",
        "inspector_ unlocked_hint": "hover any card above to inspect its full text & stats · click a build row to lock it open",
        # Actions
        "act_buy_minion": "Buy {name} (-{cost}g)",
        "act_buy_spell": "Buy spell {name} (-{cost}g)",
        "act_sell_minion": "Sell {name} (+{gain}g)",
        "act_play_minion": "Play {name}",
        "act_play_spell": "Cast {name}{cost_str}",
        "act_hero_power": "Hero Power: {name} (-{cost}g)",
        "act_upgrade": "Upgrade to T{tier} (-{cost}g)",
        "act_reorder": "Order: {order}",
        "act_freeze_targets": "Freeze shop ({targets})",
        "act_freeze": "Freeze shop",
        "act_roll": "Roll (-{cost}g) -> re-plan",
        "act_end_turn": "End turn",
        "badge_upgrade": "▲ UP",
        "badge_reroll": "↻ ROLL",
    },
}


def t(key: str, **kwargs) -> str:
    """Translate a UI/action string key into the active language."""
    table = STRINGS.get(_CURRENT_LANG, STRINGS["uk"])
    tmpl = table.get(key) or STRINGS["en"].get(key, key)
    return tmpl.format(**kwargs) if kwargs else tmpl


def tr_tribe(tribe: str) -> str:
    """Translate a single or slash-separated tribe string (e.g. 'Demon/Naga')."""
    if not tribe:
        return "—"
    if _CURRENT_LANG == "en":
        return tribe
    table = TRIBE_NAMES["uk"]
    parts = [table.get(p.strip().upper(), p.strip()) for p in tribe.split("/")]
    return "/".join(parts)


def tr_role(role: str) -> str:
    table = ROLE_NAMES.get(_CURRENT_LANG, ROLE_NAMES["uk"])
    return table.get(role.upper(), role.title())


def format_ago(sec: float) -> str:
    if _CURRENT_LANG == "uk":
        return f"{int(sec)}с" if sec < 90 else f"{int(sec // 60)}хв"
    return f"{int(sec)}s" if sec < 90 else f"{int(sec // 60)}m"


_RE_META = re.compile(r"^meta ([+-]\d+\.\d+)$")
_RE_TURN_STRENGTH = re.compile(r"^(strong|weak) at turn (\d+)$")
_RE_SHARED_TRIBE = re.compile(r"^(\d+) on board share its tribe$")
_RE_COMP_REASON = re.compile(r"^(core|addon|cycle) of (.+?) \((.+), ([SAB])\)$")
_RE_CARDS_FRAC = re.compile(r"^(\d+/\d+) cards$")
_RE_ON_TRIBE = re.compile(r"^(\d+) on tribe$")
_RE_BOARD_AVG = re.compile(r"^board (\d+%) of average$")
_RE_PLACE = re.compile(r"^place (\d+)$")
_RE_TOOK_DMG = re.compile(r"^took (\d+) dmg last (\d+)t$")
_RE_CORE_MISSING = re.compile(r"^(\d+) core piece\(s\) missing$")
_RE_NEEDS_TIER = re.compile(r"^needs tier \+(\d+)$")


def tr_reason(reason: str) -> str:
    """Translate Advisor reason / CompFit.describe / PivotAdvice strings."""
    if _CURRENT_LANG == "en" or not reason:
        return reason

    if reason == "completes triple!":
        return "збирає триплет!"
    if reason == "makes a pair":
        return "робить пару"
    if reason == "no meta data":
        return "немає мета-даних"
    if reason == "no pieces yet":
        return "ще немає карт"

    if m := _RE_META.match(reason):
        return f"мета {m.group(1)}"
    if m := _RE_TURN_STRENGTH.match(reason):
        adj = "сильна" if m.group(1) == "strong" else "слабка"
        return f"{adj} на ході {m.group(2)}"
    if m := _RE_SHARED_TRIBE.match(reason):
        return f"{m.group(1)} на столі того ж племені"
    if m := _RE_COMP_REASON.match(reason):
        role_uk = tr_role(m.group(1)).lower()
        comp_name = m.group(2)
        desc_uk = tr_fit_describe(m.group(3))
        power = m.group(4)
        return f"{role_uk} {comp_name} ({desc_uk}, {power})"

    return tr_fit_describe(reason)


def tr_fit_describe(desc: str) -> str:
    """Translate comma-separated CompFit / PivotAdvice description bits."""
    if _CURRENT_LANG == "en" or not desc:
        return desc
    if desc == "no pieces yet":
        return "ще немає карт"

    out_bits: list[str] = []
    for raw_bit in desc.split(", "):
        bit = raw_bit.strip()
        if m := _RE_CARDS_FRAC.match(bit):
            out_bits.append(f"{m.group(1)} карт")
        elif m := _RE_ON_TRIBE.match(bit):
            out_bits.append(f"{m.group(1)} за племенем")
        elif bit == "tribe not in lobby":
            out_bits.append("племені немає в лобі")
        elif bit == "tribe unconfirmed":
            out_bits.append("плем'я не підтв.")
        elif m := _RE_BOARD_AVG.match(bit):
            out_bits.append(f"стіл {m.group(1)} від середнього")
        elif m := _RE_PLACE.match(bit):
            out_bits.append(f"місце #{m.group(1)}")
        elif m := _RE_TOOK_DMG.match(bit):
            out_bits.append(f"-{m.group(1)} ХП за {m.group(2)}х")
        elif m := _RE_CORE_MISSING.match(bit):
            out_bits.append(f"бракує {m.group(1)} карт ядра")
        elif m := _RE_NEEDS_TIER.match(bit):
            out_bits.append(f"потрібен рів +{m.group(1)}")
        else:
            out_bits.append(bit)
    return ", ".join(out_bits)


def tr_verdict(verdict: str) -> str:
    if _CURRENT_LANG == "en" or not verdict:
        return verdict
    res = verdict
    res = res.replace("no benchmark yet for your hero", "ще немає еталону для героя")
    res = res.replace("ahead of pace", "попереду темпу")
    res = res.replace("on pace", "у темпі")
    res = res.replace("behind pace", "позаду темпу")
    res = re.sub(r"needs (\d+) core piece\(s\)", r"бракує \1 карт ядра", res)
    res = re.sub(r"up to tier (\d+)", r"до T\1", res)
    return res


def explain_comp_fit(fit, compact: bool = False) -> list[str]:
    """Generate human-readable explanation lines for WHY `fit` was chosen and what it needs."""
    uk = _CURRENT_LANG == "uk"
    why_parts: list[str] = []

    tribes_str = "/".join(tr_tribe(r) for r in getattr(fit, "comp_tribes", ()))
    matched_core = getattr(fit, "matched_core", ()) or ()
    matched_support = getattr(fit, "matched_support", ()) or ()
    tribe_minions = getattr(fit, "tribe_minions", ()) or ()
    missing_core = getattr(fit, "missing_core", ()) or ()

    if matched_core:
        lbl = "є ядро" if uk else "have core"
        why_parts.append(f"{lbl}: {', '.join(matched_core[:2])}")
    if matched_support:
        lbl = "є карти" if uk else "have pieces"
        why_parts.append(f"{lbl}: {', '.join(matched_support[:2])}")
    if tribe_minions and not matched_core:
        t_lbl = tribes_str or ("плем'я" if uk else "tribe")
        names = ", ".join(tribe_minions[:2])
        why_parts.append(f"{t_lbl} на столі ({names})" if uk else f"{t_lbl} on board ({names})")
    elif tribes_str and not why_parts:
        why_parts.append(f"плем'я {tribes_str} у лобі" if uk else f"{tribes_str} in lobby")

    pwr = fit.comp.power_level
    why_parts.append(f"{pwr}-тір мета" if uk else f"{pwr}-tier meta")

    why_line = ("чому: " if uk else "why: ") + " · ".join(why_parts)
    lines = [why_line]

    if missing_core:
        need_lbl = "шукай ядро: " if uk else "need core: "
        limit = 2 if compact else 3
        need_str = ", ".join(missing_core[:limit])
        if len(missing_core) > limit:
            need_str += f" (+{len(missing_core) - limit})"
        lines.append(need_lbl + need_str)
    elif matched_core:
        lines.append("усі карти ядра зібрані!" if uk else "all core pieces owned!")

    return lines
