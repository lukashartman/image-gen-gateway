"""Unit tests for the pure request-translation helpers."""

import app as gateway
from conftest import AUTH  # noqa: F401  (keeps import surface obvious)


class TestNormalizeSize:
    def test_pixels_pass_through(self):
        assert gateway.normalize_size("1024x1024") == "1024x1024"
        assert gateway.normalize_size("1920x1080") == "1920x1080"

    def test_tiers_canonicalized(self):
        assert gateway.normalize_size("2k") == "2K"
        assert gateway.normalize_size("1.5K") == "1.5K"
        assert gateway.normalize_size("512") == "512"

    def test_auto_and_empty_dropped(self):
        assert gateway.normalize_size("auto") is None
        assert gateway.normalize_size("") is None
        assert gateway.normalize_size(None) is None
        assert gateway.normalize_size("  ") is None

    def test_malformed_dropped(self):
        assert gateway.normalize_size("big") is None
        assert gateway.normalize_size("1024") is None
        assert gateway.normalize_size("10x") is None


class TestNormalizeQuality:
    def test_valid_passthrough(self):
        for q in ("auto", "low", "medium", "high"):
            assert gateway.normalize_quality(q) == q

    def test_aliases(self):
        assert gateway.normalize_quality("standard") == "medium"
        assert gateway.normalize_quality("hd") == "high"

    def test_invalid_dropped(self):
        assert gateway.normalize_quality("ultra") is None
        assert gateway.normalize_quality(None) is None


class TestNormalizeBackground:
    def test_valid_passthrough(self):
        for b in ("auto", "transparent", "opaque"):
            assert gateway.normalize_background(b) == b
        assert gateway.normalize_background("TRANSPARENT") == "transparent"

    def test_invalid_dropped(self):
        assert gateway.normalize_background("blurry") is None
        assert gateway.normalize_background(None) is None


class TestNormalizeN:
    def test_clamped(self):
        assert gateway.normalize_n(0) == 1
        assert gateway.normalize_n(-5) == 1
        assert gateway.normalize_n(99) == 10
        assert gateway.normalize_n("3") == 3
        assert gateway.normalize_n(5) == 5

    def test_missing_or_invalid(self):
        assert gateway.normalize_n(None) is None
        assert gateway.normalize_n("") is None
        assert gateway.normalize_n("lots") is None


class TestNormalizeExtras:
    def test_allowlist(self):
        assert gateway.normalize_extras(
            {"seed": 42, "user": "u1", "output_format": "png", "nope": 1}, False
        ) == {"seed": 42, "user": "u1", "output_format": "png"}

    def test_size_conflict_dropped(self):
        out = gateway.normalize_extras(
            {"resolution": "2K", "aspect_ratio": "16:9", "seed": 5}, True
        )
        assert out == {"seed": 5}

    def test_size_conflict_kept_without_size(self):
        out = gateway.normalize_extras({"resolution": "2K", "seed": 5}, False)
        assert out == {"resolution": "2K", "seed": 5}


class TestResolveModelSlug:
    def test_alias_first(self, monkeypatch):
        monkeypatch.setattr(
            gateway.settings,
            "model_aliases",
            {"dall-e-3": "google/gemini-3.1-flash-image"},
        )
        assert (
            gateway.resolve_model_slug("dall-e-3") == "google/gemini-3.1-flash-image"
        )

    def test_raw_slug(self):
        assert gateway.resolve_model_slug("openai/gpt-image-2") == "openai/gpt-image-2"

    def test_default_model(self, monkeypatch):
        monkeypatch.setattr(gateway.settings, "default_model", "openai/gpt-image-2")
        assert gateway.resolve_model_slug(None) == "openai/gpt-image-2"
        assert gateway.resolve_model_slug("") == "openai/gpt-image-2"

    def test_none_when_nothing_set(self, monkeypatch):
        monkeypatch.setattr(gateway.settings, "default_model", "")
        assert gateway.resolve_model_slug(None) is None


class TestImageStore:
    def test_capacity_evicts_oldest(self):
        store = gateway.ImageStore(capacity=2)
        a = store.put(b"aaa", "image/png", 60)
        b = store.put(b"bbb", "image/png", 60)
        c = store.put(b"ccc", "image/png", 60)
        assert store.get(a) is None
        assert store.get(b) == (b"bbb", "image/png")
        assert store.get(c) == (b"ccc", "image/png")

    def test_ttl_expiry(self, monkeypatch):
        store = gateway.ImageStore()
        fake_now = [1000.0]
        monkeypatch.setattr(gateway.time, "time", lambda: fake_now[0])
        image_id = store.put(b"zzz", "image/png", 10)
        assert store.get(image_id) == (b"zzz", "image/png")
        fake_now[0] = 2000.0
        assert store.get(image_id) is None