"""A fixed, liquid universe for backtests.

Large, heavily traded US stocks that were all listed for the whole test window,
so leaving out delisted names (survivorship bias) matters little here. It is
NOT the scanner's whole-market universe: a gap-and-go scan over thousands of
small caps would need point-in-time listings, which free data does not give.
"""

LIQUID_100 = """
AAPL MSFT NVDA AMZN GOOGL META TSLA AVGO AMD NFLX ORCL CRM ADBE INTC QCOM MU TXN AMAT LRCX KLAC
MRVL ARM SMCI PLTR SNOW CRWD PANW NOW SHOP UBER ABNB COIN MSTR HOOD SOFI PYPL XYZ DELL ANET IBM
JPM BAC WFC C GS MS SCHW BLK V MA AXP
XOM CVX COP OXY SLB HAL
UNH LLY JNJ PFE MRK ABBV AMGN GILD MRNA CVS
WMT COST HD LOW TGT NKE SBUX MCD KO PEP DIS CMCSA
BA CAT DE GE HON LMT RTX UPS FDX
F GM RIVN LCID NIO
T VZ
RBLX DKNG AFRM U
""".split()

INDEX_ETFS = ["SPY", "QQQ", "IWM", "DIA"]
