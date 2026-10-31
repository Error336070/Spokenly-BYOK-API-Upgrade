"""Usage estimates for providers that report no cost, kept apart from billed costs."""


def is_assemblyai(backend: str | None, provider_model: str | None) -> bool:
    return backend == "native" and (provider_model or "").startswith("assemblyai/")


def estimated_cost(backend: str | None, provider_model: str | None, duration_s: float | None,
                   cfg: dict) -> float | None:
    """Native AssemblyAI cost from audio duration and credit.assemblyai_hourly_rate_usd; None when unknown."""
    rate = (cfg.get("credit") or {}).get("assemblyai_hourly_rate_usd")
    if not is_assemblyai(backend, provider_model) or rate is None or duration_s is None:
        return None
    return duration_s / 3600 * rate
