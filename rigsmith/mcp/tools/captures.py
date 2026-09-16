"""NAM capture tools: search TONE3000, inspect and scan local files."""

from __future__ import annotations

from typing import Literal

from ...core import captures
from ...rig.catalog import get_catalog
from .._shared import tool


def _catalog_caveat() -> str:
    catalog = get_catalog()
    if catalog.is_verified:
        return ""
    return (
        "\n\nNote: the model catalog is UNVERIFIED - it has not been extracted "
        "from a real device, so model ids here are placeholders. Run "
        "'rigsmith catalog extract' against hardware before relying on them."
    )


@tool
def search_models(
    query: str = "",
    category: str | None = None,
    family: str | None = None,
    limit: int = 30,
) -> str:
    """Search the block catalog for amps, cabs, IRs, and effects.

    Args:
        query: Free text matched against model id, name, and subcategory.
        category: One of amp, cab, ir, clone, nam, effect.
        family: 'headrush' or 'revalver' for amps and cabs.
        limit: Maximum results.
    """
    from ...core.rigs import parse_category

    catalog = get_catalog()
    cat = parse_category(category) if category else None
    results = catalog.search(query, category=cat, family=family, limit=limit)

    if not results:
        return f"No models matched '{query}'." + _catalog_caveat()

    lines = [f"{len(results)} model(s):"]
    for model in results:
        parts = [f"  {model.id}", f"({model.category.value}"]
        if model.family:
            parts.append(f"/{model.family}")
        if model.subcategory:
            parts.append(f"/{model.subcategory}")
        parts.append(")")
        if not model.verified:
            parts.append("[unverified]")
        lines.append(" ".join(parts).replace("( ", "(").replace(" )", ")"))
    return "\n".join(lines) + _catalog_caveat()


@tool
def describe_model(model_id: str) -> str:
    """Show a model's category, family, and full parameter list with ranges."""
    catalog = get_catalog()
    model = catalog.get(model_id)
    if model is None:
        return (
            f"'{model_id}' is not in the catalog."
            + _catalog_caveat()
            + "\n\nTry search_models to see what is available."
        )

    lines = [
        f"{model.name}  (id: {model.id})",
        f"  category: {model.category.value}"
        + (f" / {model.family}" if model.family else "")
        + (f" / {model.subcategory}" if model.subcategory else ""),
        f"  verified: {model.verified}",
    ]
    if model.cpu_cost is not None:
        lines.append(f"  est. CPU: ~{model.cpu_cost:.0%}")
    if model.params:
        lines.append("  parameters:")
        for param in model.params:
            rng = ""
            if param.minimum is not None or param.maximum is not None:
                rng = f" [{param.minimum}..{param.maximum}]"
            if param.choices:
                rng = f" {{{', '.join(param.choices[:8])}}}"
            unit = f" {param.unit}" if param.unit else ""
            default = f"  default={param.default}" if param.default is not None else ""
            lines.append(f"    {param.name:20} {param.kind:6}{rng}{unit}{default}")
    else:
        lines.append("  parameters: unknown (catalog not extracted from a device)")
    return "\n".join(lines)


@tool
def tone3000_status() -> str:
    """Report whether TONE3000 is usable, and what is missing if not.

    Makes no network call - it only checks for a client id and saved tokens.
    """
    return captures.auth_status().render()


@tool
async def search_captures(
    query: str = "",
    make: str | None = None,
    tags: list[str] | None = None,
    architecture: Literal["1", "2", "any"] = "2",
    sizes: list[str] | None = None,
    calibrated: bool = False,
    limit: int = 15,
) -> str:
    """Search TONE3000 for NAM captures.

    The Core runs A2 Full and A2 Lite plus every A1 size, and A2 captures the
    amp's sag and compression better, so architecture defaults to A2.

    Requires TONE3000 credentials - run 'rigsmith tone3000 login' first, or
    check tone3000_status. Some endpoints are tier-gated; if the API refuses,
    that is reported rather than worked around.

    Args:
        query: Free text, e.g. 'JCM800 hot'.
        make: Manufacturer filter, e.g. 'Marshall'.
        tags: Tag filters.
        architecture: '2' for A2, '1' for A1, 'any' for the API default.
        sizes: e.g. ['lite'] to keep CPU cost down.
        calibrated: Only captures carrying input/output dBu levels, which can
            be gain-staged properly instead of guessed at.
        limit: Maximum results.
    """
    tones = await captures.search(
        query,
        make=make,
        tags=tags,
        architecture=architecture,
        sizes=sizes,
        calibrated=calibrated,
        limit=limit,
    )
    return captures.render_search(tones, query)


@tool
def inspect_capture(path: str) -> str:
    """Inspect a local `.nam` capture file.

    Reports architecture, generation (A1/A2), size tier, receptive field,
    sample rate, gear metadata, calibration levels, and whether the Core can
    load it - along with the evidence for each inference, since the file
    format does not state the generation outright.
    """
    return captures.render_inspection(captures.inspect(path))


@tool
def scan_captures(directory: str) -> str:
    """Parse every `.nam` file in a directory and summarise them.

    Useful against the Core's NAM folder over USB, or a local library.
    """
    return captures.render_scan(captures.scan(directory), directory)


@tool
def rank_captures(
    directory: str,
    gear_make: str | None = None,
    gear_model: str | None = None,
    tone_type: str | None = None,
) -> str:
    """Rank a local capture library against a tone target.

    Scores metadata agreement only - make, model, tone type, architecture,
    calibration. It is a shortlisting aid, not a measure of how close
    anything sounds; that requires actually rendering audio through it.
    """
    ranked = captures.rank_local(
        directory, gear_make=gear_make, gear_model=gear_model, tone_type=tone_type
    )
    if not ranked:
        return f"No usable captures under {directory}."

    lines = [f"{len(ranked)} capture(s), best metadata match first:"]
    for model, score, reasons in ranked[:15]:
        lines.append(f"  {score:5.1f}  {model.name or '(unnamed)'}")
        lines.extend(f"         - {r}" for r in reasons)
    lines.append(
        "\nMetadata agreement only. Render these through a DI and score them "
        "before choosing - search_offline does that in one step."
    )
    return "\n".join(lines)
