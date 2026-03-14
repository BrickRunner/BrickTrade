#!/bin/bash
export EXCHANGES=okx,htx,bybit
export ENABLED_STRATEGIES=triangular_arbitrage,multi_triangular_arbitrage,funding_spread
python3 main.py
