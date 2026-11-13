"""Pure helpers for the Xray config template kept by 3x-ui (ad blocking rule, YouTube exit outbound and rule)."""

from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from app.services.tg_only_data import TELEGRAM_CIDRS, TELEGRAM_DOMAINS

BLOCK_TAG: Final = "blocked"
ADS_GEOSITE: Final = "geosite:category-ads-all"
API_TAG: Final = "api"
YOUTUBE_TAG: Final = "youtube-exit"
YOUTUBE_BALANCER_TAG: Final = "youtube-balancer"
DIRECT_TAG: Final = "direct"
PROBE_URL: Final = "https://www.google.com/generate_204"
PROBE_INTERVAL: Final = "1m"
TG_ONLY_IP_TAG: Final = "tg-only-ip"
TG_ONLY_DOMAIN_TAG: Final = "tg-only-domain"
TG_ONLY_BLOCK_TAG: Final = "tg-only-block"
TG_ONLY_RULE_TAGS: Final = frozenset({TG_ONLY_IP_TAG, TG_ONLY_DOMAIN_TAG, TG_ONLY_BLOCK_TAG})


@dataclass(frozen=True, slots=True)
class YoutubeExit:
    """The wanted exit: a ready Xray outbound (tagged ``youtube-exit``) and the domains routed through it."""

    outbound: dict[str, Any]
    domains: tuple[str, ...]


class TemplateError(ValueError):
    """The template cannot be edited safely (unexpected shape or a conflicting outbound)."""


def ad_rule() -> dict[str, Any]:
    return {"type": "field", "domain": [ADS_GEOSITE], "outboundTag": BLOCK_TAG}


def parse_template(obj: Any) -> dict[str, Any]:
    """The Xray template out of a ``/xray/`` answer (a JSON string, or an object wrapping ``xraySetting``)."""
    value = obj
    for _ in range(4):  # strings and wrapper objects can be nested by older panel versions
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError as exc:
                raise TemplateError("Xray template is not valid JSON") from exc
        elif isinstance(value, dict) and "xraySetting" in value:
            value = value["xraySetting"]
        else:
            break
    if not isinstance(value, dict):
        raise TemplateError("Xray template has an unexpected shape")
    return value


def _routing_rules(template: dict[str, Any]) -> list[Any]:
    routing = template.get("routing")
    if routing is None:
        routing = template["routing"] = {}
    if not isinstance(routing, dict):
        raise TemplateError("Xray template: 'routing' is not an object")
    rules = routing.get("rules")
    if rules is None:
        rules = routing["rules"] = []
    if not isinstance(rules, list):
        raise TemplateError("Xray template: 'routing.rules' is not a list")
    return rules


def _outbounds(template: dict[str, Any]) -> list[Any]:
    outbounds = template.get("outbounds")
    if outbounds is None:
        outbounds = template["outbounds"] = []
    if not isinstance(outbounds, list):
        raise TemplateError("Xray template: 'outbounds' is not a list")
    return outbounds


def has_blackhole(template: dict[str, Any]) -> bool:
    return any(
        isinstance(o, dict) and o.get("tag") == BLOCK_TAG and o.get("protocol") == "blackhole"
        for o in _outbounds(template)
    )


def ad_blocking_enabled(template: dict[str, Any]) -> bool:
    """True when our rule is present and the ``blocked`` blackhole outbound exists."""
    rule = ad_rule()
    return any(r == rule for r in _routing_rules(template)) and has_blackhole(template)


def _api_index(rules: list[Any]) -> int:
    """Right after the last panel API rule: the earliest place an operator rule may take."""
    index = 0
    for i, rule in enumerate(rules):
        if isinstance(rule, dict) and rule.get("outboundTag") == API_TAG:
            index = i + 1
    return index


def _is_tg_only(rule: Any) -> bool:
    return isinstance(rule, dict) and rule.get("ruleTag") in TG_ONLY_RULE_TAGS


def _insert_index(rules: list[Any]) -> int:
    """Where the ad and YouTube rules go: after the API rules and after the Telegram-only block, which stays first
    so nothing can let the Telegram-only client out (first match wins)."""
    index = _api_index(rules)
    while index < len(rules) and _is_tg_only(rules[index]):
        index += 1
    return index


def set_ad_blocking(template: dict[str, Any], on: bool) -> tuple[dict[str, Any], bool]:
    """Return ``(new_template, changed)``. Only our rule (matched by its exact shape) is added or removed."""
    result = copy.deepcopy(template)
    rules = _routing_rules(result)
    rule = ad_rule()
    changed = False
    if on:
        outbounds = _outbounds(result)
        tagged = [o for o in outbounds if isinstance(o, dict) and o.get("tag") == BLOCK_TAG]
        if not tagged:
            outbounds.append({"tag": BLOCK_TAG, "protocol": "blackhole", "settings": {}})
            changed = True
        elif tagged[0].get("protocol") != "blackhole":
            raise TemplateError(f"outbound '{BLOCK_TAG}' exists but is not a blackhole")
        if rule not in rules:
            rules.insert(_insert_index(rules), rule)
            changed = True
    else:
        kept = [r for r in rules if r != rule]
        if len(kept) != len(rules):
            rules[:] = kept
            changed = True
    return result, changed


# ---------------------------------------------------------------------------------------------
# YouTube exit
# ---------------------------------------------------------------------------------------------
def _is_ours(rule: Any) -> bool:
    return isinstance(rule, dict) and (
        rule.get("outboundTag") == YOUTUBE_TAG or rule.get("balancerTag") == YOUTUBE_BALANCER_TAG
    )


def _direct_tag(template: dict[str, Any]) -> str | None:
    """Tag of the freedom outbound used as the fallback (the panel's ``direct`` one, else any freedom outbound)."""
    freedom = [o for o in _outbounds(template) if isinstance(o, dict) and o.get("protocol") == "freedom"]
    for outbound in freedom:
        if outbound.get("tag") == DIRECT_TAG:
            return DIRECT_TAG
    tags = [o.get("tag") for o in freedom if isinstance(o.get("tag"), str) and o.get("tag")]
    return tags[0] if tags else None


def _replace_tagged(items: list[Any], tag: str, new: dict[str, Any] | None) -> list[Any]:
    """``items`` with the entries tagged ``tag`` replaced in place by ``new`` (appended if absent; dropped if None)."""
    out: list[Any] = []
    placed = False
    for item in items:
        if isinstance(item, dict) and item.get("tag") == tag:
            if new is not None and not placed:
                out.append(new)
                placed = True
            continue
        out.append(item)
    if new is not None and not placed:
        out.append(new)
    return out


def _youtube_rule(domains: tuple[str, ...], balanced: bool) -> dict[str, Any]:
    rule: dict[str, Any] = {"type": "field", "domain": list(domains)}
    if balanced:
        rule["balancerTag"] = YOUTUBE_BALANCER_TAG
    else:
        rule["outboundTag"] = YOUTUBE_TAG
    return rule


def _routing(template: dict[str, Any]) -> dict[str, Any]:
    _routing_rules(template)  # creates and validates the section
    routing: dict[str, Any] = template["routing"]
    return routing


def _balancers(template: dict[str, Any]) -> list[Any]:
    routing = _routing(template)
    balancers = routing.get("balancers")
    if balancers is None:
        balancers = routing["balancers"] = []
    if not isinstance(balancers, list):
        raise TemplateError("Xray template: 'routing.balancers' is not a list")
    return balancers


def _our_observatory() -> dict[str, Any]:
    return {
        "subjectSelector": [YOUTUBE_TAG],
        "probeUrl": PROBE_URL,
        "probeInterval": PROBE_INTERVAL,
        "enableConcurrency": True,
    }


def _observer_key(template: dict[str, Any]) -> str:
    """The observer section to extend: an operator's own one if present, else ``observatory``."""
    for key in ("observatory", "burstObservatory"):
        if isinstance(template.get(key), dict):
            return key
    return "observatory"


def youtube_exit_enabled(template: dict[str, Any], wanted: YoutubeExit | None) -> bool:
    """True when the template already holds exactly what ``wanted`` asks for (``None``: none of our entries)."""
    return not set_youtube_exit(template, wanted)[1]


def set_youtube_exit(template: dict[str, Any], wanted: YoutubeExit | None) -> tuple[dict[str, Any], bool]:
    """Return ``(new_template, changed)``. Only entries tagged ``youtube-exit`` / ``youtube-balancer`` are touched.

    With a fallback-capable template (a freedom outbound exists) the rule goes through a balancer whose
    ``fallbackTag`` is that freedom outbound, backed by an observatory probe: when the exit is down, YouTube
    takes the normal route. Otherwise the rule points at the exit directly.
    """
    base = copy.deepcopy(template)
    _routing_rules(base)
    _outbounds(base)
    had_balancers = "balancers" in base["routing"]
    if had_balancers:
        _balancers(base)  # validates the shape
    result = copy.deepcopy(base)
    rules = _routing_rules(result)
    outbounds = _outbounds(result)
    balancers = _balancers(result)
    key = _observer_key(result)

    kept_rules = [r for r in rules if not _is_ours(r)]
    kept_out = _replace_tagged(outbounds, YOUTUBE_TAG, copy.deepcopy(wanted.outbound) if wanted else None)
    kept_bal = list(balancers)
    fallback = _direct_tag(result) if wanted is not None else None

    observer = result.get(key)
    if isinstance(observer, dict) and isinstance(observer.get("subjectSelector"), list):
        selector: list[Any] = observer["subjectSelector"]
        present = YOUTUBE_TAG in selector
        if wanted is not None and fallback is not None:
            if not present:
                selector.append(YOUTUBE_TAG)
        elif present:
            selector[:] = [t for t in selector if t != YOUTUBE_TAG]
            if not selector and observer.get("probeUrl") == PROBE_URL:
                del result[key]  # the section was ours
    elif wanted is not None and fallback is not None:
        result[key] = _our_observatory()

    if wanted is not None:
        index = _insert_index(kept_rules)
        ad = ad_rule()
        for i, rule in enumerate(kept_rules):
            if rule == ad:
                index = max(index, i + 1)
        kept_rules.insert(index, _youtube_rule(wanted.domains, fallback is not None))
    balancer = {"tag": YOUTUBE_BALANCER_TAG, "selector": [YOUTUBE_TAG], "fallbackTag": fallback} if fallback else None
    kept_bal = _replace_tagged(balancers, YOUTUBE_BALANCER_TAG, balancer)
    rules[:] = kept_rules
    outbounds[:] = kept_out
    balancers[:] = kept_bal
    had_ours = any(
        isinstance(b, dict) and b.get("tag") == YOUTUBE_BALANCER_TAG for b in base["routing"].get("balancers", [])
    )
    if not kept_bal and (not had_balancers or had_ours):
        del result["routing"]["balancers"]
    return result, result != base


def apply_extras(template: dict[str, Any], *, ads: bool, youtube: YoutubeExit | None) -> tuple[dict[str, Any], bool]:
    """Ad blocking and the YouTube exit in one pass (the ad rule first, so the exit rule lands after it)."""
    first, changed_ads = set_ad_blocking(template, ads)
    second, changed_yt = set_youtube_exit(first, youtube)
    return second, changed_ads or changed_yt


# ---------------------------------------------------------------------------------------------
# Telegram-only clients
# ---------------------------------------------------------------------------------------------
def tg_only_rules(emails: Sequence[str], direct_tag: str) -> list[dict[str, Any]]:
    """The three rules that confine the clients ``emails`` to Telegram: its addresses, its domains, then a block."""
    users = list(emails)
    return [
        {
            "type": "field",
            "ruleTag": TG_ONLY_IP_TAG,
            "user": users,
            "ip": list(TELEGRAM_CIDRS),
            "outboundTag": direct_tag,
        },
        {
            "type": "field",
            "ruleTag": TG_ONLY_DOMAIN_TAG,
            "user": users,
            "domain": list(TELEGRAM_DOMAINS),
            "outboundTag": direct_tag,
        },
        {"type": "field", "ruleTag": TG_ONLY_BLOCK_TAG, "user": users, "outboundTag": BLOCK_TAG},
    ]


def set_tg_only(template: dict[str, Any], emails: Sequence[str]) -> tuple[dict[str, Any], bool]:
    """Return ``(new_template, changed)``. Only rules tagged ``tg-only-*`` are touched; no emails removes them.

    The block goes right after the panel API rules, ahead of every other rule, and the ``blocked`` blackhole
    outbound is created when missing. Safe to repeat: the result is identical when nothing changes.
    """
    routing = template.get("routing")
    present = isinstance(routing, dict) and any(_is_tg_only(r) for r in routing.get("rules") or [])
    if not emails and not present:
        return copy.deepcopy(template), False
    result = copy.deepcopy(template)
    rules = _routing_rules(result)
    kept = [r for r in rules if not _is_tg_only(r)]
    if emails:
        direct = _direct_tag(result)
        if direct is None:
            raise TemplateError("the template has no 'freedom' outbound to let Telegram traffic out")
        outbounds = _outbounds(result)
        tagged = [o for o in outbounds if isinstance(o, dict) and o.get("tag") == BLOCK_TAG]
        if not tagged:
            outbounds.append({"tag": BLOCK_TAG, "protocol": "blackhole", "settings": {}})
        elif tagged[0].get("protocol") != "blackhole":
            raise TemplateError(f"outbound '{BLOCK_TAG}' exists but is not a blackhole")
        at = _api_index(kept)
        kept[at:at] = tg_only_rules(sorted(set(emails)), direct)
    rules[:] = kept
    return result, result != template


def tg_only_enabled(template: dict[str, Any], emails: Sequence[str]) -> bool:
    """True when the template already holds exactly the rules ``emails`` ask for."""
    return not set_tg_only(template, emails)[1]
