"""Walk-forward backtesting (D39(4) / O7).

Reads stored data only — no network, and nothing here is in the daily
pipeline. The fill rules are NOT defined in this package: `resolve.py` hands
every plan to `shadow_book.outcome_tracker.evaluate_call`, so what a backtest
says happened to a trade is what the live Shadow Book would say.
"""
