def build(platform, settings, state_dir):
    """Return (broker, market_data) for a platform. In paper mode orders are simulated."""
    if platform == "coindcx":
        from .coindcx import CoinDCX
        live = CoinDCX(settings)
    elif platform == "zerodha":
        from .zerodha import Zerodha
        live = Zerodha(settings, state_dir)
    else:
        raise ValueError(f"unknown platform {platform!r}")

    if settings.mode == "live":
        return live, live
    from .paper import PaperBroker
    return PaperBroker(platform, live, state_dir, settings.paper_cash), live
