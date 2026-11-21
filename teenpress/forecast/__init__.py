"""需求预测子包：信号处理、可解释模型、滚动回测与预测服务。"""
from .backtest import backtest_series, choose_model, rolling_cutoffs
from .models import MODEL_LABELS, MODEL_REGISTRY, fit_model
from .service import forecast_book, forecast_to_dict
from .signals import build_signals

__all__ = [
    "backtest_series", "choose_model", "rolling_cutoffs",
    "MODEL_LABELS", "MODEL_REGISTRY", "fit_model",
    "forecast_book", "forecast_to_dict", "build_signals",
]
