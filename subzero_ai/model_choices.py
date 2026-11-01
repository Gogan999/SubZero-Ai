"""Which models a person can pick, in one place: the Settings dropdowns, the mentor's New chat and model picker,
and the check each reply makes before it uses a chat's model.

A model is offered only when its provider is turned on and has a key (Claude subscription: signed in), the active
backend can run it (on `anthropic` only Claude models), `check_model` allows it (Fable and Mythos are kept off), and,
for chats and the screener, it has passed the Model safety check. The Settings page also lists models that haven't
been checked yet, marked so, because saving still refuses them."""

from __future__ import annotations

from typing import Any

from .ai_backend import resolve_backend
from .config import MODEL_ID_SETTINGS, PROVIDERS, Settings, model_provider, normalize_model_id
from .db import Database
from .model_check import latest_result, latest_results, requirement_problem
from .pi_client import BRIDGE_PROVIDER, bridge_logged_in, provider_keys
from .saved_settings import PROVIDER_LABELS, check_model, judge_ok

MODEL_LABELS = {
    "anthropic/claude-sonnet-5-5": "Sonnet 5.5",
    "anthropic/claude-opus-5-5": "Opus 5.5",
    "anthropic/claude-haiku-5-5": "Haiku 5.5",
}
# What each model setting is for. chat covers the default and hard-problem models (and mentors' picks).
PURPOSES = ("chat", "default", "hard", "screener", "title", "judge")
MODEL_PURPOSE = {"model_default": "default", "model_hard": "hard", "model_titles": "title",
                 "model_screener": "screener", "model_check_judge": "judge"}
# Purposes that need the Model safety check, and which part of it (True: the screener part)
CHECKED_PURPOSES = {"chat": False, "default": False, "hard": False, "screener": True}


def model_label(model_id: str, names: dict[str, str] | None = None) -> str:
    """A short name for a model: the built-in label, else the name the gateway gave it (Pi backend), else the part
    of the id after the provider."""
    model_id = normalize_model_id(model_id)
    return MODEL_LABELS.get(model_id) or (names or {}).get(model_id) or model_id.split("/", 1)[-1]


def cost_hint(info: dict | None) -> str:
    """What the gateway says a model costs per million tokens, or '' when it reports nothing (it sends 0 then)."""
    cost = (info or {}).get("cost") or {}
    try:
        cost_in, cost_out = float(cost.get("input") or 0), float(cost.get("output") or 0)
    except (TypeError, ValueError):
        return ""
    if not (cost_in or cost_out):
        return ""
    return f"about ${cost_in:g} in, ${cost_out:g} out per million tokens"


def option_label(option: dict[str, Any]) -> str:
    """The text a dropdown shows for an option: its name, and its cost when the gateway reports one."""
    return f"{option['label']} ({option['cost_hint']})" if option["cost_hint"] else option["label"]


def _candidates(services: Any) -> list[str]:
    """Every model the app knows about: the built-in labels, the gateway's list, models ever checked, and the ones
    set now. Settings only lists a model when its provider is on and keyed, so extra names are harmless."""
    ids: list[str] = list(MODEL_LABELS) + list(services.model_info)
    ids += [row["model"] for row in latest_results(services.db)]
    ids += [getattr(services.settings, name) for name in MODEL_ID_SETTINGS]
    return list(dict.fromkeys(normalize_model_id(model) for model in ids if model))


def _rules_problem(settings: Settings, model: str, purpose: str) -> str:
    """Why a model can't be used for this purpose, ignoring the safety check; '' when it can."""
    backend = resolve_backend(settings)
    _, problem = check_model(model, settings.ai_providers, backend)
    if problem:
        return problem
    provider, _, name = model.partition("/")
    if backend == "anthropic" and not name.startswith("claude-"):
        return "Only Claude models work with AI_BACKEND=anthropic."
    if purpose == "judge" and (provider != "anthropic" or not judge_ok(model)):
        return "The safety check judge has to be an Anthropic Claude model."
    if provider == BRIDGE_PROVIDER:
        if not bridge_logged_in(settings.data_dir):
            return "Sign in to the Claude subscription first, under AI and keys."
    elif provider not in provider_keys(settings):
        return f"Add the {PROVIDER_LABELS.get(provider, provider)} API key first."
    return ""


def _check_problem(db: Database, model: str, purpose: str, settings: Settings | None = None) -> str:
    """Why the Model safety check doesn't allow this purpose yet, or '' (purposes without a check always pass).
    The models set now as default and hard-problem keep working without a check: they ran before the check existed,
    and the check only gates a change (saving a new one still needs it). A check that ran and failed still blocks."""
    if purpose not in CHECKED_PURPOSES:
        return ""
    if settings is not None and not CHECKED_PURPOSES[purpose] and model in {
            normalize_model_id(settings.model_default), normalize_model_id(settings.model_hard)} and latest_result(db, model) is None:
        return ""  # never checked: allowed. A check that ran and failed still blocks it
    return requirement_problem(db, model, screener=CHECKED_PURPOSES[purpose])


def model_block(settings: Settings, db: Database, model: str, purpose: str = "chat") -> str:
    """The strict test: why this model can't be used for the purpose now, or '' when it can. New chat, the mentor's
    model picker and every reply go through this."""
    model = normalize_model_id(model)
    return _rules_problem(settings, model, purpose) or _check_problem(db, model, purpose, settings)


def selectable_models(services: Any, *, purpose: str, include_unchecked: bool = False) -> list[dict[str, Any]]:
    """The models to offer for a purpose, in provider order. Each option has provider, id, label, cost_hint, checked
    (passed the safety check the purpose needs), usable (same as checked: it can be picked now) and note (why it
    isn't checked). Unchecked models are left out unless include_unchecked (Settings lists them, marked)."""
    if purpose not in PURPOSES:
        raise ValueError(purpose)
    settings, db = services.settings, services.db
    options: list[dict[str, Any]] = []
    for model in _candidates(services):
        if _rules_problem(settings, model, purpose):
            continue
        problem = _check_problem(db, model, purpose, settings)
        if problem and not include_unchecked:
            continue
        note = ""
        if problem:
            note = "not yet safety-checked" if latest_result(db, model) is None else "didn't pass the safety check"
        options.append({"provider": model_provider(model), "id": model,
                        "label": model_label(model, services.model_names),
                        "cost_hint": cost_hint(services.model_info.get(model)),
                        "checked": not problem, "usable": not problem, "note": note})
    order = {provider: i for i, provider in enumerate(PROVIDERS)}
    return sorted(options, key=lambda o: (order.get(o["provider"], len(order)), o["label"].lower()))


def group_options(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Options grouped by provider, for <optgroup>s: [{label, options}] in the order given."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for option in options:
        groups.setdefault(option["provider"], []).append(option)
    return [{"label": PROVIDER_LABELS.get(provider, provider), "options": items} for provider, items in groups.items()]
