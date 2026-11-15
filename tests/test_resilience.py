from __future__ import annotations

from types import SimpleNamespace

from app.config import DEFAULTS, load_config
from app.db.models import Quote
from app.db.repo import Repo
from app.db.session import Database
from app.services.intraday import IntradaySampler, MinuteFeed, SamplePoint
from app.services.intraday_poller import IntradayPoller
from app.services.quote_poller import QuotePoller


def test_invalid_config_uses_isolated_defaults(tmp_path, recwarn):
    config_file = tmp_path / "broken.yaml"
    config_file.write_text("datasource: [", encoding="utf-8")

    config = load_config(config_file)

    assert config["datasource"] == DEFAULTS["datasource"]
    assert any("使用默认配置" in str(warning.message) for warning in recwarn)
    config["datasource"]["name"] = "changed"
    assert DEFAULTS["datasource"]["name"] != "changed"


def test_quote_poller_continues_after_one_symbol_processing_error(tmp_path):
    class Adapter:
        poller: QuotePoller | None = None

        def get_quote(self, symbol):
            if symbol == "good" and self.poller is not None:
                self.poller.stop()
            return Quote(symbol=symbol, name=symbol, price=10, prev_close=9)

    class Sampler(IntradaySampler):
        def feed(self, quote, now=None) -> SamplePoint | None:
            if quote.symbol == "bad":
                raise ValueError("bad sample")
            return super().feed(quote, now)

    adapter = Adapter()
    repo = Repo(Database(tmp_path / "unused.db"))
    poller = QuotePoller(adapter, Sampler(), repo=repo, persist=False)
    adapter.poller = poller
    poller.update(["bad", "good"])

    poller.run()

    results = poller.drain()
    assert len(results) == 1
    assert [quote.symbol for quote in results[0].quotes] == ["bad", "good"]
    assert any("bad sample" in error for error in results[0].errors)


def test_intraday_poller_continues_after_one_symbol_processing_error(tmp_path):
    class Adapter:
        poller: IntradayPoller | None = None

        def trends(self, symbol):
            if symbol == "good" and self.poller is not None:
                self.poller.stop()
            return SimpleNamespace(
                bars=[], name=symbol, pre_close=9, data_level="B",
            )

    class Feed(MinuteFeed):
        def update(self, trends):
            if trends.name == "bad":
                raise ValueError("bad trend")
            return [], [], []

    adapter = Adapter()
    repo = Repo(Database(tmp_path / "unused.db"))
    poller = IntradayPoller(
        adapter, Feed(), repo=repo, persist=False, is_trading=lambda: True,
    )
    adapter.poller = poller
    poller.update(["bad", "good"])

    poller.run()

    updates = poller.drain()
    assert [update.symbol for update in updates] == ["bad", "good"]
    assert updates[0].ok is False
    assert updates[0].error is not None and "bad trend" in updates[0].error
    assert updates[1].ok is True
