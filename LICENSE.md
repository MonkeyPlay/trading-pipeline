# License

## MIT License

Copyright (c) 2026 Andreas Vrhovsek

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

---

## Third-party notices

The MIT grant above covers the original code in this repository. One vendored file
carries its own terms, which govern that file and are **not** superseded by the MIT
license above.

### TradingView Lightweight Charts™ — Apache-2.0

`dashboard/components/lib/lightweight-charts.standalone.js` is a vendored, unmodified
build of TradingView's Lightweight Charts v5.2.1.

> Copyright (c) 2026 TradingView, Inc.
> Licensed under the Apache License 2.0 — https://www.apache.org/licenses/LICENSE-2.0

"TradingView" and "Lightweight Charts" are trademarks of TradingView, Inc. Nothing in
this project is affiliated with or endorsed by TradingView.

### Removed: the Pine indicator ports

Earlier versions carried Python ports of two TradingView Pine indicators, which
brought two obligations with them: an MPL-2.0 file-level copyleft on the "TEMA &
Session Levels" port (© Ununseptium), and a script of unverifiable provenance behind
"Auto Anchored VWAP". Both were deleted along with the `indicator/` package, so
neither applies to this project any more. Every remaining source file is original work
under the MIT license above.

If you are working from an older checkout or a fork that still contains
`indicator/tema_session.py`, that file remains MPL-2.0 there and its terms still apply
to it.

---

## No warranty, and not financial advice

This is research tooling, not trading infrastructure. Nothing in it places orders, and
nothing it produces is investment advice or a recommendation to trade. Market data
retrieved through Interactive Brokers remains subject to your agreements with IBKR and
the relevant exchanges; this license grants no rights to that data. You are solely
responsible for any trading decisions and any losses arising from them.
