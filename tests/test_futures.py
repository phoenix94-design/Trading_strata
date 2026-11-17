import pytest

from strata.futures import price_levels as pl
from strata.futures.price_levels import PriceLevelBot, Setup

pl.time.sleep = lambda s: None          # don't wait for positions in tests


def long_setup(**kw):
    base = dict(name="L", pair="B-BTC_USDT", side="long", entry_price=100, take_profit=110,
                stop_loss=95, quantity=1, leverage=3)
    return Setup(**{**base, **kw})


class FakeClient:
    """Stands in for CoinDCXFutures. Set .price; it records every call."""

    def __init__(self, price=120.0, tpsl_fails=False):
        self.price, self.pos, self.calls, self.tpsl_fails = price, None, [], tpsl_fails

    def last_price(self, pair):
        return self.price

    def market_order(self, pair, side, qty, lev):
        self.calls.append(("order", side, qty, lev))
        self.pos = {"id": "P1", "pair": pair, "active_pos": qty if side == "buy" else -qty, "avg_price": self.price}

    def open_position(self, pair):
        return self.pos

    def set_tpsl(self, pid, tp, sl, fmt):
        self.calls.append(("tpsl", pid, fmt(tp), fmt(sl)))
        if self.tpsl_fails:
            raise RuntimeError("rejected")

    def exit_position(self, pid):
        self.calls.append(("exit", pid))
        self.pos = None


def bot(tmp_path, setups, client, mode="paper"):
    return PriceLevelBot(setups, client, mode, tmp_path, tmp_path, fee_pct=0.05)


def test_validation():
    with pytest.raises(ValueError):
        long_setup(stop_loss=105)                       # stop above entry for a long
    with pytest.raises(ValueError):
        long_setup(side="short")                        # short needs tp < entry < sl
    with pytest.raises(ValueError):
        long_setup(leverage=20)
    with pytest.raises(ValueError):
        long_setup(stop_loss=60, leverage=5)            # stop beyond liquidation distance
    s = Setup(name="S", pair="X", side="short", entry_price=100, take_profit=90, stop_loss=104, quantity=1)
    assert s.entry_when == "at_or_above"


def test_paper_long_waits_enters_and_takes_profit(tmp_path):
    c = FakeClient(price=120)
    b = bot(tmp_path, [long_setup()], c)
    b.tick()
    assert b.state["L"].phase == "waiting"
    c.price = 99.5
    b.tick()
    assert b.state["L"].phase == "open" and b.state["L"].entry_fill == 99.5
    c.price = 111
    b.tick()
    st = b.state["L"]
    assert st.phase == "done" and st.trades == 1 and st.realized_pnl == pytest.approx(10.5 - 0.0005 * (99.5 + 110))
    assert c.calls == []                                # paper mode never touches the exchange


def test_paper_short_stop_and_repeat(tmp_path):
    s = Setup(name="S", pair="X", side="short", entry_price=100, take_profit=90, stop_loss=104,
              quantity=2, repeat=True)
    c = FakeClient(price=101)
    b = bot(tmp_path, [s], c)
    b.tick()
    assert b.state["S"].phase == "open"
    c.price = 105
    b.tick()
    assert b.state["S"].phase == "waiting" and b.state["S"].realized_pnl < 0   # re-armed after the stop


def test_does_not_enter_if_already_past_stop(tmp_path):
    c = FakeClient(price=94)                            # below entry AND below stop
    b = bot(tmp_path, [long_setup()], c)
    b.tick()
    assert b.state["L"].phase == "waiting"


def test_state_survives_restart(tmp_path):
    c = FakeClient(price=99)
    bot(tmp_path, [long_setup()], c).tick()
    b2 = bot(tmp_path, [long_setup()], c)
    assert b2.state["L"].phase == "open"


def test_live_entry_places_tpsl_on_exchange(tmp_path):
    c = FakeClient(price=99)
    b = bot(tmp_path, [long_setup()], c, mode="live")
    b.tick()
    assert c.calls == [("order", "buy", 1, 3), ("tpsl", "P1", "110.00", "95.00")]
    c.pos, c.price = None, 110.2                        # CoinDCX's take-profit fired
    b.tick()
    assert b.state["L"].phase == "done"


def test_live_closes_position_if_tpsl_fails(tmp_path):
    c = FakeClient(price=99, tpsl_fails=True)
    b = bot(tmp_path, [long_setup()], c, mode="live")
    with pytest.raises(RuntimeError):
        b.tick()
    assert ("exit", "P1") in c.calls and c.pos is None and b.state["L"].phase == "waiting"


def test_live_backup_stop(tmp_path):
    c = FakeClient(price=99)
    b = bot(tmp_path, [long_setup()], c, mode="live")
    b.tick()
    c.price = 90                                        # way through the stop, position still open
    b.tick()
    assert ("exit", "P1") in c.calls and b.state["L"].phase == "done"


def test_live_restart_adopts_unknown_position(tmp_path):
    c = FakeClient(price=120)
    c.pos = {"id": "P9", "pair": "B-BTC_USDT", "active_pos": 1, "avg_price": 100}
    b = bot(tmp_path, [long_setup()], c, mode="live")
    b.tick()
    assert b.state["L"].phase == "open" and ("tpsl", "P9", "110.00", "95.00") in c.calls
    assert not any(x[0] == "order" for x in c.calls)    # never doubles up


def market_setup(**kw):
    base = dict(name="M", pair="B-BTC_USDT", side="long", entry_price="market", take_profit=64100,
                stop_loss=64000, quantity=1, leverage=10, price_decimals=1)
    return Setup(**{**base, **kw})


def test_market_entry_enters_now_and_exits(tmp_path):
    c = FakeClient(price=64050)
    b = bot(tmp_path, [market_setup()], c)
    b.tick()
    assert b.state["M"].phase == "open" and b.state["M"].entry_fill == 64050
    c.price = 64101
    b.tick()
    st = b.state["M"]
    assert st.phase == "done" and st.realized_pnl == pytest.approx(50 - 0.0005 * (64050 + 64100))


def test_market_entry_waits_while_price_outside_range(tmp_path):
    c = FakeClient(price=64128.5)                       # above take profit: a long makes no sense
    b = bot(tmp_path, [market_setup()], c)
    b.tick()
    assert b.state["M"].phase == "waiting"
    c.price = 64060
    b.tick()
    assert b.state["M"].phase == "open"


def test_market_entry_checks_liquidation_at_entry(tmp_path):
    c = FakeClient(price=70000)
    b = bot(tmp_path, [market_setup(take_profit=80000, stop_loss=60000)], c)   # 14% stop at 10x
    b.tick()
    assert b.state["M"].phase == "waiting"


def pct_setup(**kw):
    base = dict(name="P", pair="B-BTC_USDT", side="long", entry_price="market", take_profit="+0.1%",
                stop_loss="-0.1%", quantity=0.01, leverage=10, price_decimals=1)
    return Setup(**{**base, **kw})


def test_percent_levels_follow_the_fill(tmp_path):
    c = FakeClient(price=64000)
    b = bot(tmp_path, [pct_setup()], c)
    b.tick()
    st = b.state["P"]
    assert st.phase == "open" and st.tp == pytest.approx(64064) and st.sl == pytest.approx(63936)
    c.price = 63930
    b.tick()
    assert b.state["P"].phase == "done" and b.state["P"].realized_pnl < 0


def test_percent_levels_live_tpsl_prices(tmp_path):
    c = FakeClient(price=64000)
    b = bot(tmp_path, [pct_setup()], c, mode="live")
    b.tick()
    assert ("tpsl", "P1", "64064.0", "63936.0") in c.calls


def test_percent_levels_validation():
    with pytest.raises(ValueError):
        pct_setup(take_profit="-0.1%", stop_loss="+0.1%")      # wrong way round for a long
    with pytest.raises(ValueError):
        pct_setup(stop_loss="-9%")                               # beyond liquidation at 10x
    pct_setup(side="short", take_profit="-0.1%", stop_loss="+0.1%")
