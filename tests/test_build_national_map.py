"""The one integration point in the batch script worth a direct test: that
coarse mode actually swaps the noise/air providers and leaves everything else
alone. Each provider's own coarse behaviour (which layers, correctness,
absence-handling) is already covered in tests/test_rivm_noise.py and
tests/test_rivm_air.py — this only checks the assembly.
"""
from __future__ import annotations

from scripts.build_national_map import _build_pipeline
from app.services.enrichment.enrichment_pipeline import EnrichmentPipeline
from app.services.enrichment.providers.rivm_air import COARSE_LAYERS as COARSE_AIR
from app.services.enrichment.providers.rivm_noise import COARSE_LAYERS as COARSE_NOISE


def test_full_mode_uses_the_pipelines_own_defaults():
    pipeline = _build_pipeline(http=None, coarse=False)

    assert pipeline.providers["noise"].layers != COARSE_NOISE
    assert pipeline.providers["air"].layers != COARSE_AIR


def test_coarse_mode_swaps_only_noise_and_air():
    pipeline = _build_pipeline(http=None, coarse=True)

    assert pipeline.providers["noise"].layers == COARSE_NOISE
    assert pipeline.providers["air"].layers == COARSE_AIR
    # Every other provider is still the pipeline's own real default class.
    for key, cls in EnrichmentPipeline.PROVIDER_MAP.items():
        if key in ("noise", "air"):
            continue
        assert isinstance(pipeline.providers[key], cls)


def test_coarse_mode_covers_every_provider_the_pipeline_expects():
    """A missing key here would silently SKIP a whole layer for every buurt
    in the country, not just narrow it — the assembly must be complete."""
    pipeline = _build_pipeline(http=None, coarse=True)
    assert set(pipeline.providers) == set(EnrichmentPipeline.PROVIDER_MAP)
