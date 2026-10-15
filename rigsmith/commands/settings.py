"""``rigsmith settings`` - mode, models, defaults."""

from __future__ import annotations

import json

import click

from ..clikit import handled


@click.group("settings")
def group() -> None:
    """App settings: accuracy or local mode, which model runs the agents."""


@group.command("show")
@handled
def settings_show() -> None:
    """Everything, as JSON."""
    from ..core import appsettings

    data = appsettings.load().to_dict()
    data.pop("modes", None)
    click.echo(json.dumps(data, indent=2))


@group.command("mode")
@click.argument("mode", type=click.Choice(["accuracy", "local"]))
@handled
def settings_mode(mode: str) -> None:
    """accuracy (Claude, network allowed) or local (nothing leaves this machine)."""
    from ..core import appsettings

    appsettings.update(mode=mode)
    click.echo(f"mode: {mode} - {appsettings.MODES[mode]}")


@group.command("llm")
@click.option("--local", "which", flag_value="local_llm", help="Set the local-mode model.")
@click.option("--accuracy", "which", flag_value="llm", default=True, help="Set the accuracy-mode model.")
@click.option("--provider", type=click.Choice(["auto", "claude-code", "anthropic", "openai-compatible"]))
@click.option("--url", "base_url", help="OpenAI-compatible endpoint, e.g. http://127.0.0.1:1234/v1")
@click.option("--model")
@click.option("--key-env", "api_key_env", help="Environment variable holding an API key.")
@click.option("--thinking/--no-thinking", default=None)
@handled
def settings_llm(which, provider, base_url, model, api_key_env, thinking) -> None:
    """Choose the model behind the chat and the agents."""
    from ..core import appsettings

    changes = {k: v for k, v in {"provider": provider, "base_url": base_url, "model": model, "api_key_env": api_key_env, "thinking": thinking}.items() if v is not None}
    settings = appsettings.update(**{which: changes})
    click.echo(json.dumps(getattr(settings, which).__dict__, indent=2))


@group.command("set")
@click.argument("name")
@click.argument("value")
@handled
def settings_set(name: str, value: str) -> None:
    """Set one plain setting: default_profile, default_route, audio_backend..."""
    from ..core import appsettings

    parsed: object = value
    if value.lower() in ("true", "false"):
        parsed = value.lower() == "true"
    elif value.lower() in ("none", "null", ""):
        parsed = None
    appsettings.update(**{name: parsed})
    click.echo(f"{name} = {parsed}")
